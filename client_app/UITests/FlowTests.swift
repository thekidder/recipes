import XCTest

final class FlowTests: XCTestCase {
    func testSharedURLUsesAutomaticDelivery() {
        let app = XCUIApplication()
        app.launchArguments = ["--demo", "--demo-url-share"]
        app.launch()
        XCTAssertTrue(app.staticTexts["Selected for the reader’s next refresh."].waitForExistence(timeout: 20))
        XCTAssertFalse(app.buttons["Send to reader"].exists)
        screenshot("URL delivery")
    }
    func testPastedRecipeLinkSendsAutomatically() {
        let app = XCUIApplication()
        app.launchArguments = ["--demo"]
        app.launch()
        let paste = app.buttons["Paste recipe link"]
        XCTAssertTrue(paste.waitForExistence(timeout: 10)); paste.tap()
        let field = app.textFields["Recipe URL"]
        XCTAssertTrue(field.waitForExistence(timeout: 5))
        field.tap(); field.typeText("https://example.com/recipe#method")
        app.buttons["Share recipe"].tap()
        XCTAssertTrue(app.staticTexts["Selected for the reader’s next refresh."].waitForExistence(timeout: 20))
    }
    func testSharedImageUsesAutomaticDelivery() {
        let app = XCUIApplication()
        app.launchArguments = ["--demo", "--demo-image-share"]
        app.launch()
        XCTAssertTrue(app.staticTexts["Selected for the reader’s next refresh."].waitForExistence(timeout: 20))
        XCTAssertFalse(app.buttons["Send to reader"].exists)
        screenshot("Image delivery")
    }
    private func screenshot(_ name: String) {
        let attachment = XCTAttachment(screenshot: XCUIScreen.main.screenshot())
        attachment.name = name; attachment.lifetime = .keepAlways; add(attachment)
    }
    func testPastedRecipeSendsAutomatically() {
        let app = XCUIApplication()
        app.launchArguments = ["--demo"]
        app.launch()
        XCTAssertTrue(app.staticTexts["Demo — no server or reader is contacted"].waitForExistence(timeout: 10))
        screenshot("Home")
        app.buttons["Paste recipe text"].tap()
        let editor = app.textViews["Recipe text"]
        XCTAssertTrue(editor.waitForExistence(timeout: 5))
        editor.tap(); editor.typeText("Lemon chickpea salad. Mix chickpeas with lemon juice. Serve chilled.")
        app.buttons["Share recipe"].tap()
        XCTAssertTrue(app.staticTexts["Selected for the reader’s next refresh."].waitForExistence(timeout: 15))
        XCTAssertFalse(app.buttons["Send to reader"].exists)
        screenshot("Selected")
    }
    func testSharedInputSendsWithoutAnyButtonPress() {
        let app = XCUIApplication()
        app.launchArguments = ["--demo", "--demo-share"]
        app.launch()
        // The exact ImportView used by the extension receives an already shared input.
        // There are deliberately no taps between launch and the publication receipt.
        XCTAssertTrue(app.staticTexts["Selected for the reader’s next refresh."].waitForExistence(timeout: 20))
        XCTAssertFalse(app.buttons["Import & preview"].exists)
        XCTAssertFalse(app.buttons["Send to reader"].exists)
        screenshot("Automatic delivery")
    }
}
