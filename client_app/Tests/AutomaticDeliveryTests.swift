import XCTest
@testable import ERecipeCore

extension CoreTests {
    private func automaticShare() throws -> PendingShare {
        let item = try store.create(input: ShareInput(kind: "text", text: "A shared recipe"), connection: connection, limits: status().capabilities.limits)
        try store.update(item.id) { $0.jobId = "job_extract"; $0.phase = "processing" }
        return item
    }
    private func renderedJob() throws -> Data {
        var body = try JSONSerialization.jsonObject(with: fixture("job_extracted")) as! [String: Any]
        body["id"] = "job_render"; body["kind"] = "render"; body["downstream_job_ids"] = []
        body["result"] = ["revision_id": "revision_initial", "render_set_id": "render_initial"]
        return try JSONSerialization.data(withJSONObject: body)
    }
    func testAutomaticDeliveryWaitsForRenderThenPublishesPageOne() throws {
        let item = try automaticShare()
        let delivery = AutomaticDelivery(store: store, connection: connection)
        var transfer = try XCTUnwrap(delivery.initial(item.id))
        transfer = try XCTUnwrap(delivery.consume(transfer, data: fixture("status")))
        transfer = try XCTUnwrap(delivery.consume(transfer, data: fixture("job_extracted")))
        XCTAssertNil(try store.item(item.id)?.publishKey)
        var queued = try JSONSerialization.jsonObject(with: fixture("job_queued")) as! [String: Any]
        queued["id"] = "job_render"
        let waiting = try XCTUnwrap(delivery.consume(transfer, data: JSONSerialization.data(withJSONObject: queued)))
        XCTAssertGreaterThan(waiting.delay, 0)
        XCTAssertNil(try store.item(item.id)?.renderSet)
        transfer = try XCTUnwrap(delivery.consume(waiting, data: renderedJob()))
        transfer = try XCTUnwrap(delivery.consume(transfer, data: fixture("render_set")))
        transfer = try XCTUnwrap(delivery.consume(transfer, data: fixture("status")))
        XCTAssertEqual(transfer.step, .publication)
        let (body, key) = try delivery.publicationBody(item.id)
        let payload = try JSONSerialization.jsonObject(with: body) as! [String: Any]
        XCTAssertEqual(payload["page_number"] as? Int, 1)
        XCTAssertEqual(payload["render_set_id"] as? String, "render_initial")
        XCTAssertNotNil(UUID(uuidString: key))
        XCTAssertNil(try delivery.consume(transfer, data: fixture("publication_selected")))
        XCTAssertEqual(try store.item(item.id)?.phase, "selected")
        XCTAssertNil(try delivery.initial(item.id))
    }
    func testAutomaticDeliverySurvivesRestartAndDoesNotResendConfirmedReceipt() throws {
        let item = try automaticShare()
        try store.update(item.id) { $0.renderSet = try Wire.decoder.decode(RenderSet.self, from: fixture("render_set")) }
        let original = AutomaticDelivery(store: store, connection: connection)
        let (_, key) = try original.publicationBody(item.id)
        let reloaded = try SharedStore(root: root)
        let resumed = AutomaticDelivery(store: reloaded, connection: connection)
        let step = DeliveryTransfer(itemId: item.id, step: .publication)
        XCTAssertEqual(DeliveryTransfer(description: step.description)?.step, .publication)
        XCTAssertEqual(try resumed.publicationBody(item.id).1, key)
        XCTAssertNil(try resumed.consume(step, data: fixture("publication_selected")))
        XCTAssertNil(try resumed.initial(item.id))
        XCTAssertNil(try resumed.consume(step, data: fixture("publication_selected")))
    }
    func testAutomaticDeliveryStopsOnFailureAndProtectsOldImportsAndAccounts() throws {
        let legacy = try pending()
        try store.update(legacy.id) { $0.jobId = "job_extract"; $0.autoSend = nil }
        let delivery = AutomaticDelivery(store: store, connection: connection)
        XCTAssertNil(try delivery.initial(legacy.id))
        let item = try automaticShare()
        let job = DeliveryTransfer(itemId: item.id, step: .job("job_extract", remaining: [], visited: []))
        XCTAssertNil(try delivery.consume(job, data: fixture("job_configuration_missing")))
        XCTAssertEqual(try store.item(item.id)?.phase, "failed")
        XCTAssertNil(try store.item(item.id)?.publishKey)
        try store.clear()
        XCTAssertNil(try delivery.initial(item.id))
    }
    func testAutomaticDeliveryRejectsIncompleteRenderAndDisabledPublication() throws {
        let item = try automaticShare()
        let delivery = AutomaticDelivery(store: store, connection: connection)
        var statusBody = try JSONSerialization.jsonObject(with: fixture("status")) as! [String: Any]
        var capabilities = statusBody["capabilities"] as! [String: Any]; capabilities["publication"] = false; statusBody["capabilities"] = capabilities
        XCTAssertThrowsError(try delivery.consume(DeliveryTransfer(itemId: item.id, step: .status), data: JSONSerialization.data(withJSONObject: statusBody)))
        var render = try JSONSerialization.jsonObject(with: fixture("render_set")) as! [String: Any]
        render["page_count"] = 2
        XCTAssertThrowsError(try delivery.consume(DeliveryTransfer(itemId: item.id, step: .render("render_initial")), data: JSONSerialization.data(withJSONObject: render)))
        XCTAssertNil(try store.item(item.id)?.publishKey)
    }
}
