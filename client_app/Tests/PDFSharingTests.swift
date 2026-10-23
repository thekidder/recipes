import XCTest
import UniformTypeIdentifiers
@testable import ERecipeCore

extension CoreTests {
    private var pdfBytes: Data { Data("%PDF-1.7\nReader recipe bytes\n%%EOF".utf8) }

    private func pdfProvider(_ data: Data? = nil) -> NSItemProvider {
        let provider = NSItemProvider()
        provider.suggestedName = "Lemon salad.pdf"
        let bytes = data ?? pdfBytes
        provider.registerDataRepresentation(forTypeIdentifier: UTType.pdf.identifier, visibility: .all) { completion in
            completion(bytes, nil)
            return nil
        }
        return provider
    }

    func testReaderPDFWinsOverSafariURLAndPersistsOriginalBytes() async throws {
        let provider = pdfProvider()
        provider.registerItem(forTypeIdentifier: UTType.url.identifier, loadHandler: { completion, _, _ in
            XCTFail("A Reader PDF must not fall back to the website URL")
            completion?(URL(string: "https://example.com/blocked")! as NSURL, nil)
        })
        let input = try await SharedInputLoader.loadProviders([provider])
        XCTAssertEqual(input.kind, "pdf")
        XCTAssertEqual(input.pdf?.data, pdfBytes)
        XCTAssertNil(input.url)
        XCTAssertEqual(input.summary, "Lemon salad.pdf")
        var limits = try status().capabilities.limits
        limits.pdfFileBytes = PDFInput.maxBytes
        limits.pdfPages = 50
        let item = try store.create(input: input, connection: connection, limits: limits)
        let body = try Data(contentsOf: store.bodyURL(item.id))
        XCTAssertTrue(item.contentType.hasPrefix("multipart/form-data; boundary="))
        XCTAssertNotNil(body.range(of: pdfBytes))
        let wire = String(decoding: body, as: UTF8.self)
        XCTAssertTrue(wire.contains("name=\"pdf\""))
        XCTAssertTrue(wire.contains("Content-Type: application/pdf"))
        XCTAssertTrue(wire.contains("\"kind\":\"pdf\""))
        XCTAssertFalse(wire.contains("https://example.com"))
        let reopened = try SharedStore(root: store.root)
        XCTAssertEqual(try reopened.item(item.id)?.importKey, item.importKey)
        XCTAssertEqual(try Data(contentsOf: reopened.bodyURL(item.id)), body)
    }

    func testReaderPDFWithSeparateURLCompanion() async throws {
        let url = NSItemProvider(item: URL(string: "https://example.com/recipe")! as NSURL, typeIdentifier: UTType.url.identifier)
        let input = try await SharedInputLoader.loadProviders([url, pdfProvider()])
        XCTAssertEqual(input.kind, "pdf")
        XCTAssertEqual(input.pdf?.data, pdfBytes)
    }

    func testPDFFileURLRepresentations() async throws {
        let url = store.root.appendingPathComponent("Recipe.PDF")
        try pdfBytes.write(to: url)
        for type in [UTType.fileURL, UTType.url] {
            let provider = NSItemProvider(item: url as NSURL, typeIdentifier: type.identifier)
            let input = try await SharedInputLoader.loadProviders([provider])
            XCTAssertEqual(input.kind, "pdf")
            XCTAssertEqual(input.pdf?.data, pdfBytes)
        }
    }

    func testPDFLimitsAndUnsupportedServer() async throws {
        let input = ShareInput(kind: "pdf", pdf: PDFInput(filename: "Recipe.pdf", data: pdfBytes))
        var limits = try status().capabilities.limits
        XCTAssertThrowsError(try input.requestBody(limits: limits))
        limits.pdfFileBytes = 5
        XCTAssertThrowsError(try input.requestBody(limits: limits))
        XCTAssertThrowsError(try PDFInput(filename: "fake.pdf", data: Data("not a PDF".utf8)).validate())
        for providers in [[pdfProvider(), pdfProvider()], [pdfProvider(), NSItemProvider(item: "recipe text" as NSString, typeIdentifier: UTType.plainText.identifier)], [pdfProvider(Data("not a PDF".utf8))]] {
            do {
                _ = try await SharedInputLoader.loadProviders(providers)
                XCTFail("Invalid or mixed PDF input should fail")
            } catch {}
        }
    }
}
