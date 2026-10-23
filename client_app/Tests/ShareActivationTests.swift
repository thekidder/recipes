import Foundation
import XCTest

final class ShareActivationTests: XCTestCase {
    private func activationRule() throws -> NSPredicate {
        let root = URL(fileURLWithPath: #filePath).deletingLastPathComponent().deletingLastPathComponent()
        let data = try Data(contentsOf: root.appendingPathComponent("ShareExtension/Info.plist"))
        let plist = try XCTUnwrap(PropertyListSerialization.propertyList(from: data, format: nil) as? [String: Any])
        let ext = try XCTUnwrap(plist["NSExtension"] as? [String: Any])
        let attributes = try XCTUnwrap(ext["NSExtensionAttributes"] as? [String: Any])
        let rule = try XCTUnwrap(attributes["NSExtensionActivationRule"] as? String)
        return NSPredicate(format: rule)
    }

    private func offered(_ items: [[[String]]]) -> [String: Any] {
        ["extensionItems": items.map { attachments in
            ["attachments": attachments.map { ["registeredTypeIdentifiers": $0] }]
        }]
    }

    func testPDFActivatesWithoutFileURLAndWithAlternativeRepresentations() throws {
        let rule = try activationRule()
        for types in [["com.adobe.pdf"], ["com.adobe.pdf", "public.file-url"],
                      ["com.adobe.pdf", "public.url", "public.html"],
                      ["com.adobe.pdf", "com.apple.private.example-representation"]] {
            XCTAssertTrue(rule.evaluate(with: offered([[types]])), "Missing PDF share: \(types)")
        }
        // A companion website URL can be a second attachment or extension item.
        XCTAssertTrue(rule.evaluate(with: offered([[["com.adobe.pdf"], ["public.url"]]])))
        XCTAssertTrue(rule.evaluate(with: offered([[["com.adobe.pdf"]], [["public.url"]]])))
    }

    func testExistingShareTypesStillActivate() throws {
        let rule = try activationRule()
        for type in ["public.url", "public.file-url", "public.utf8-plain-text", "public.jpeg", "public.png", "public.heic"] {
            XCTAssertTrue(rule.evaluate(with: offered([[[type]]])), "Missing share type: \(type)")
        }
        XCTAssertTrue(rule.evaluate(with: offered([Array(repeating: ["public.jpeg"], count: 6)])))
    }

    func testEmptyUnsupportedAndExcessiveSharesDoNotActivate() throws {
        let rule = try activationRule()
        XCTAssertFalse(rule.evaluate(with: offered([])))
        XCTAssertFalse(rule.evaluate(with: offered([[]])))
        XCTAssertFalse(rule.evaluate(with: offered([[["public.movie"]]])))
        XCTAssertFalse(rule.evaluate(with: offered([[["com.adobe.pdf"], ["public.movie"]]])))
        XCTAssertFalse(rule.evaluate(with: offered([Array(repeating: ["public.jpeg"], count: 7)])))
    }
}
