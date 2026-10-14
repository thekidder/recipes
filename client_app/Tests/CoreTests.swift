import XCTest
import ImageIO
import UniformTypeIdentifiers
@testable import ERecipeCore

final class StubProtocol: URLProtocol, @unchecked Sendable {
    static var handler: ((URLRequest) throws -> (Int, Data))!
    override class func canInit(with request: URLRequest) -> Bool { true }
    override class func canonicalRequest(for request: URLRequest) -> URLRequest { request }
    override func startLoading() {
        do {
            let (code, data) = try Self.handler(request)
            client?.urlProtocol(self, didReceive: HTTPURLResponse(url: request.url!, statusCode: code, httpVersion: nil, headerFields: [:])!, cacheStoragePolicy: .notAllowed)
            client?.urlProtocol(self, didLoad: data); client?.urlProtocolDidFinishLoading(self)
        } catch { client?.urlProtocol(self, didFailWithError: error) }
    }
    override func stopLoading() {}
}

final class CoreTests: XCTestCase {
    var root: URL!
    var store: SharedStore!
    let connection = Connection(origin: URL(string: "https://recipes.example.invalid")!, serverId: "server_demo", user: Person(id: "user_alex", displayName: "Alex"), credentialId: "credential_phone")
    override func setUpWithError() throws {
        root = FileManager.default.temporaryDirectory.appendingPathComponent(UUID().uuidString)
        store = try SharedStore(root: root); try store.setConnection(connection)
    }
    override func tearDownWithError() throws { store = nil; try FileManager.default.removeItem(at: root); StubProtocol.handler = nil }
    func fixture(_ name: String) throws -> Data {
        let url = Bundle.module.url(forResource: name, withExtension: "json", subdirectory: "Fixtures")!
        let envelope = try JSONSerialization.jsonObject(with: Data(contentsOf: url)) as! [String: Any]
        return try JSONSerialization.data(withJSONObject: (envelope["response"] as! [String: Any])["body"]!)
    }
    func status() throws -> ServerStatus { try Wire.decoder.decode(ServerStatus.self, from: fixture("status")) }
    func pending() throws -> PendingShare { try store.create(input: ShareInput(kind: "text", text: "A recipe"), connection: connection, limits: status().capabilities.limits, autoSend: false) }
    func api() -> APIClient {
        let config = URLSessionConfiguration.ephemeral; config.protocolClasses = [StubProtocol.self]
        return APIClient(origin: connection.origin, token: "test-token", session: URLSession(configuration: config))
    }
    func testEnrollmentValidation() throws {
        var parts = URLComponents(); parts.scheme = "erecipe"; parts.host = "enroll"
        parts.queryItems = [URLQueryItem(name: "v", value: "1"), URLQueryItem(name: "server", value: connection.origin.absoluteString), URLQueryItem(name: "code", value: String(repeating: "A", count: 43))]
        XCTAssertEqual(try EnrollmentLink(parts.url!).origin, connection.origin)
        for extra in ["&v=1", "&extra=x", "#fragment"] { XCTAssertThrowsError(try EnrollmentLink(URL(string: parts.url!.absoluteString + extra)!)) }
        for origin in ["http://host", "https://host/", "https://user@host", "https://host:0", "https://host:65536", "https://host/path", "https://host?x=1", "https://host#x"] { XCTAssertThrowsError(try EnrollmentLink.validateOrigin(origin), origin) }
        parts.queryItems![0].value = "2"
        XCTAssertThrowsError(try EnrollmentLink(parts.url!))
    }
    func testWireFixtures() throws {
        _ = try Wire.decoder.decode(EnrollmentReceipt.self, from: fixture("redeem_qr"))
        _ = try status()
        _ = try Wire.decoder.decode(ImportReceipt.self, from: fixture("import_text"))
        _ = try Wire.decoder.decode(Job.self, from: fixture("job_extracted"))
        _ = try Wire.decoder.decode(Job.self, from: fixture("job_configuration_missing"))
        _ = try Wire.decoder.decode(Revision.self, from: fixture("revision"))
        _ = try Wire.decoder.decode(RenderSet.self, from: fixture("render_set"))
        _ = try Wire.decoder.decode(PublicationReceipt.self, from: fixture("publication_selected"))
        _ = try Wire.decoder.decode(Device.self, from: fixture("device_selected"))
        _ = try Wire.decoder.decode(APIError.self, from: fixture("reused_qr"))
    }
    func testRequestSecurityAndEnrollmentHasNoToken() throws {
        let client = api()
        for path in ["https://evil.invalid/api/v1/assets/a", "//evil.invalid/api/v1/a", "/control/v1/users", "/api/v1/../../control/v1/users", "/api/v1/a#secret"] { XCTAssertThrowsError(try client.request(path), path) }
        let enrollment = APIClient(origin: connection.origin)
        XCTAssertNil(try enrollment.request("/api/v1/enrollments/redeem", method: "POST").value(forHTTPHeaderField: "Authorization"))
        XCTAssertEqual(try client.request("/api/v1/status").value(forHTTPHeaderField: "Authorization"), "Bearer test-token")
    }
    func testOriginalTextAndURLArePreservedAndLimitsCountScalars() throws {
        let limits = try status().capabilities.limits
        let original = "  ½ cup café\nMix.\n"
        let (body, _) = try ShareInput(kind: "text", text: original).requestBody(limits: limits)
        XCTAssertEqual((try JSONSerialization.jsonObject(with: body) as! [String: String])["text"], original)
        let url = "https://example.com/recipe?q=a%20b#method"
        let (urlBody, _) = try ShareInput(kind: "url", url: url).requestBody(limits: limits)
        XCTAssertEqual((try JSONSerialization.jsonObject(with: urlBody) as! [String: String])["url"], url)
        XCTAssertThrowsError(try ShareInput(kind: "text", text: " \n").requestBody(limits: limits))
        XCTAssertThrowsError(try ShareInput(kind: "text", text: String(repeating: "e\u{301}", count: 50_001)).requestBody(limits: limits))
        XCTAssertThrowsError(try ShareInput(kind: "url", url: "file:///tmp/recipe").requestBody(limits: limits))
    }
    func testPersistenceAndAccountIsolation() throws {
        let original = try pending()
        let reopened = try SharedStore(root: root)
        XCTAssertEqual(try reopened.item(original.id)?.importKey, original.importKey)
        XCTAssertTrue(FileManager.default.fileExists(atPath: reopened.bodyURL(original.id).path))
        try reopened.update(original.id) { $0.phase = "processing" }
        XCTAssertEqual(try store.item(original.id)?.phase, "processing")
        let other = Connection(origin: connection.origin, serverId: "other", user: connection.user, credentialId: "other")
        try store.setConnection(other)
        XCTAssertThrowsError(try reopened.update(original.id) { $0.phase = "selected" })
        XCTAssertTrue(try reopened.items(context: other.context).isEmpty)
        try store.clear()
        XCTAssertNil(try reopened.connection())
        XCTAssertFalse(FileManager.default.fileExists(atPath: reopened.bodyURL(original.id).path))
    }
    func testExtractionSuccessDoesNotMeanReady() async throws {
        let item = try pending()
        try store.update(item.id) { $0.jobId = "job_extract" }
        StubProtocol.handler = { request in
            if request.url!.path.hasSuffix("job_extract") { return (200, try self.fixture("job_extracted")) }
            var body = try JSONSerialization.jsonObject(with: self.fixture("job_queued")) as! [String: Any]
            body["id"] = "job_render"
            return (200, try JSONSerialization.data(withJSONObject: body))
        }
        try await Workflow(store: store, api: api(), connection: connection).advance(item.id)
        XCTAssertNil(try store.item(item.id)?.renderSet)
        XCTAssertEqual(try store.item(item.id)?.phase, "processing")
    }
    func testCompletedRenderUsesExactRevisionAndFailedJobsStayRecoverable() async throws {
        let item = try pending(); try store.update(item.id) { $0.jobId = "job_extract" }
        StubProtocol.handler = { request in
            let path = request.url!.path
            if path.hasSuffix("job_extract") { return (200, try self.fixture("job_extracted")) }
            if path.hasSuffix("job_render") {
                var body = try JSONSerialization.jsonObject(with: self.fixture("job_extracted")) as! [String: Any]
                body["id"] = "job_render"; body["downstream_job_ids"] = []; body["result"] = ["revision_id":"revision_initial", "render_set_id":"render_initial"]
                return (200, try JSONSerialization.data(withJSONObject: body))
            }
            return (200, try self.fixture(path.contains("render-sets") ? "render_set" : "revision"))
        }
        try await Workflow(store: store, api: api(), connection: connection).advance(item.id)
        XCTAssertEqual(try store.item(item.id)?.renderSet?.revisionId, "revision_initial")
        XCTAssertEqual(try store.item(item.id)?.phase, "ready")
        let failed = try pending(); try store.update(failed.id) { $0.jobId = "job_extract" }
        StubProtocol.handler = { _ in (200, try self.fixture("job_configuration_missing")) }
        try await Workflow(store: store, api: api(), connection: connection).advance(failed.id)
        XCTAssertEqual(try store.item(failed.id)?.retryJobId, "job_extract")
    }
    func testAmbiguousPublicationReusesKeyAndPreventsPageChange() async throws {
        let item = try pending()
        let render = try Wire.decoder.decode(RenderSet.self, from: fixture("render_set"))
        try store.update(item.id) { $0.renderSet = render; $0.phase = "ready" }
        var keys = [String]()
        StubProtocol.handler = { request in
            if request.url!.path.hasSuffix("status") { return (200, try self.fixture("status")) }
            keys.append(request.value(forHTTPHeaderField: "Idempotency-Key")!)
            if keys.count == 1 { throw URLError(.networkConnectionLost) }
            return (201, try self.fixture("publication_selected"))
        }
        let workflow = Workflow(store: store, api: api(), connection: connection)
        do { try await workflow.publish(item.id, page: 1); XCTFail("Expected lost response") } catch {}
        XCTAssertNotNil(try store.item(item.id)?.publishKey)
        try await workflow.publish(item.id, page: 1)
        XCTAssertEqual(keys.count, 2); XCTAssertEqual(keys[0], keys[1])
        XCTAssertEqual(try store.item(item.id)?.publication?.publication.id, "publication_initial")
    }
    func testConcurrentStoreUpdatesDoNotLoseChangesAndRollbackOnError() throws {
        let item = try pending()
        let other = try SharedStore(root: root)
        DispatchQueue.concurrentPerform(iterations: 30) { index in
            do { try (index % 2 == 0 ? store! : other).update(item.id) { $0.message = ($0.message ?? "") + "x" } }
            catch { XCTFail(error.localizedDescription) }
        }
        XCTAssertEqual(try store.item(item.id)?.message?.count, 30)
        XCTAssertThrowsError(try store.update(item.id) { value in value.message = "changed"; throw ClientError("rollback") })
        XCTAssertEqual(try other.item(item.id)?.message?.count, 30)
    }
    func testMultipartHasOrderedManifestAndMatchingParts() throws {
        let input = ShareInput(kind: "images", images: [
            ImageInput(filename: "first.png", mediaType: "image/png", data: try imageFixture(.png)),
            ImageInput(filename: "second.jpg", mediaType: "image/jpeg", data: try imageFixture(.jpeg))
        ])
        let (data, type) = try input.requestBody(limits: status().capabilities.limits)
        let body = String(decoding: data, as: UTF8.self)
        XCTAssertTrue(type.hasPrefix("multipart/form-data; boundary="))
        XCTAssertTrue(body.contains("first.png")); XCTAssertTrue(body.contains("second.jpg"))
        XCTAssertTrue(body.contains("name=\"image_1\"")); XCTAssertTrue(body.contains("name=\"image_2\""))
        XCTAssertLessThan(data.range(of: input.images[0].data)!.lowerBound, data.range(of: input.images[1].data)!.lowerBound)
        let item = try store.create(input: input, connection: connection, limits: status().capabilities.limits)
        let reopened = try SharedStore(root: root)
        let savedBody = try Data(contentsOf: reopened.bodyURL(item.id))
        XCTAssertNotNil(savedBody.range(of: input.images[0].data))
        XCTAssertNotNil(savedBody.range(of: input.images[1].data))
        XCTAssertEqual(try reopened.item(item.id)?.importKey, item.importKey)
    }
    func testImagePreparationPreservesPNGAndJPEG() throws {
        for type in [UTType.png, .jpeg] {
            let data = try imageFixture(type)
            let image = try ImagePreparation.prepare(data, index: 1)
            XCTAssertEqual(image.data, data)
            XCTAssertEqual(image.mediaType, type == .png ? "image/png" : "image/jpeg")
            XCTAssertNotNil(ImagePreparation.thumbnail(image.data))
        }
    }
    func testHEICConversionAppliesOrientation() throws {
        let data = try imageFixture(.heic, width: 80, height: 40, orientation: 6)
        let image = try ImagePreparation.prepare(data, index: 2)
        XCTAssertEqual(image.filename, "recipe-2.jpg")
        XCTAssertEqual(image.mediaType, "image/jpeg")
        let source = try XCTUnwrap(CGImageSourceCreateWithData(image.data as CFData, nil))
        let decoded = try XCTUnwrap(CGImageSourceCreateImageAtIndex(source, 0, nil))
        XCTAssertEqual(decoded.width, 40); XCTAssertEqual(decoded.height, 80)
        try ImagePreparation.validate(image, pixelLimit: 20_000_000)
    }
    func testLargeCameraImageIsReducedToUploadLimits() throws {
        let data = try imageFixture(.jpeg, width: 6000, height: 4000)
        let image = try ImagePreparation.prepare(data, index: 1)
        let source = try XCTUnwrap(CGImageSourceCreateWithData(image.data as CFData, nil))
        let decoded = try XCTUnwrap(CGImageSourceCreateImageAtIndex(source, 0, nil))
        XCTAssertEqual(decoded.width, 4096)
        XCTAssertLessThan(decoded.height, 4096)
        XCTAssertLessThanOrEqual(image.data.count, ImagePreparation.maxUploadBytes)
    }
    func testImagesRejectAnimationCorruptionMIMEAndServerLimits() throws {
        XCTAssertThrowsError(try ImagePreparation.prepare(Data("not an image".utf8), index: 1))
        XCTAssertThrowsError(try ImagePreparation.prepare(imageFixture(.png, frames: 2), index: 1))
        let jpeg = try imageFixture(.jpeg)
        XCTAssertThrowsError(try ImagePreparation.prepare(jpeg.prefix(jpeg.count / 2), index: 1))
        let limits = try status().capabilities.limits
        let image = try ImagePreparation.prepare(imageFixture(.png), index: 1)
        XCTAssertThrowsError(try ImagePreparation.validate(image, pixelLimit: 1))
        XCTAssertThrowsError(try ShareInput(kind: "images", images: [ImageInput(filename: "bad.jpg", mediaType: "image/jpeg", data: image.data)]).requestBody(limits: limits))
        XCTAssertThrowsError(try ShareInput(kind: "images", images: Array(repeating: image, count: 7)).requestBody(limits: limits))
        XCTAssertThrowsError(try ShareInput(kind: "images", images: []).requestBody(limits: limits))
    }
    func testSharedImageProvidersPreserveOrderAndConvertHEIC() async throws {
        let png = try imageFixture(.png)
        let heic = try imageFixture(.heic)
        let first = NSItemProvider()
        first.registerDataRepresentation(forTypeIdentifier: UTType.png.identifier, visibility: .all) { completion in
            completion(png, nil); return nil
        }
        let second = NSItemProvider()
        second.registerDataRepresentation(forTypeIdentifier: UTType.heic.identifier, visibility: .all) { completion in
            completion(heic, nil); return nil
        }
        let input = try await SharedInputLoader.loadProviders([first, second])
        XCTAssertEqual(input.kind, "images")
        XCTAssertEqual(input.images.map(\.mediaType), ["image/png", "image/jpeg"])
        XCTAssertEqual(input.images[0].data, png)
        XCTAssertEqual(input.images.map(\.filename), ["recipe-1.png", "recipe-2.jpg"])
    }
    func testFilesImageSharesAndMixedContentRejection() async throws {
        let png = try imageFixture(.png)
        let file = root.appendingPathComponent("recipe.png")
        try png.write(to: file)
        let provider = NSItemProvider(item: file as NSURL, typeIdentifier: UTType.fileURL.identifier)
        let input = try await SharedInputLoader.loadProviders([provider])
        XCTAssertEqual(input.images.first?.data, png)
        let textFile = root.appendingPathComponent("recipe.txt")
        try Data("Recipe instructions".utf8).write(to: textFile)
        let textProvider = NSItemProvider(item: textFile as NSURL, typeIdentifier: UTType.fileURL.identifier)
        let textInput = try await SharedInputLoader.loadProviders([textProvider])
        XCTAssertEqual(textInput.text, "Recipe instructions")
        do { _ = try await SharedInputLoader.loadProviders([provider, textProvider]); XCTFail("Expected mixed files to be rejected") }
        catch { XCTAssertTrue(error.localizedDescription.contains("without other file types")) }
        do { _ = try await SharedInputLoader.loadProviders(Array(repeating: provider, count: 7)); XCTFail("Expected image count limit") }
        catch { XCTAssertTrue(error.localizedDescription.contains("six")) }
    }
    func testLostJobRetryResponsePreservesKeyAndSendsNoBody() async throws {
        let item = try pending()
        try store.update(item.id) { $0.jobId = "job_extract"; $0.retryJobId = "job_extract"; $0.phase = "failed" }
        var keys = [String]()
        StubProtocol.handler = { request in
            XCTAssertNil(request.httpBody); XCTAssertNil(request.httpBodyStream)
            keys.append(request.value(forHTTPHeaderField: "Idempotency-Key")!)
            if keys.count == 1 { throw URLError(.networkConnectionLost) }
            return (202, try self.fixture("retry_stage"))
        }
        let workflow = Workflow(store: store, api: api(), connection: connection)
        do { try await workflow.retry(item.id); XCTFail("Expected lost response") } catch {}
        try await workflow.retry(item.id)
        XCTAssertEqual(keys.count, 2); XCTAssertEqual(keys[0], keys[1])
        XCTAssertNil(try store.item(item.id)?.retryKey)
        XCTAssertEqual(try store.item(item.id)?.phase, "processing")
    }
    func testServerIdentityMismatchIsRejected() async throws {
        StubProtocol.handler = { _ in (200, try self.fixture("status")) }
        let wrong = Connection(origin: connection.origin, serverId: "other", user: connection.user, credentialId: "other")
        do { _ = try await api().status(for: wrong); XCTFail("Expected mismatch") } catch {}
    }
}
