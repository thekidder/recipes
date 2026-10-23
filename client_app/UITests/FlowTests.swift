import XCTest

final class FlowTests: XCTestCase {
    private func launch(_ app: XCUIApplication) {
        app.launchArguments.append("--demo-reset")
        app.launch()
        if !app.launchArguments.contains(where: { ["--demo-share", "--demo-image-share", "--demo-url-share"].contains($0) }) {
            app.tabBars.buttons["Add & activity"].tap()
        }
    }
    func testLibrarySearchReadAndDelete() {
        let app = XCUIApplication()
        app.launchArguments = ["--demo"]
        launch(app)
        app.tabBars.buttons["Recipes"].tap()
        let recipe = app.buttons["library-recipe-recipe_salad"]
        XCTAssertTrue(recipe.waitForExistence(timeout: 10))
        screenshot("Recipe library")
        let search = app.searchFields.firstMatch
        search.tap(); search.typeText("no matching recipe")
        XCTAssertFalse(recipe.exists)
        app.buttons["Clear text"].tap()
        if app.buttons["Close"].exists { app.buttons["Close"].tap() }
        else { app.buttons["Cancel"].tap() }
        recipe.tap()
        XCTAssertTrue(app.staticTexts["recipe-title"].waitForExistence(timeout: 10))
        XCTAssertTrue(app.staticTexts["Ingredients"].exists)
        let ingredient = app.buttons.matching(NSPredicate(format: "label CONTAINS %@", "2 cans chickpeas")).firstMatch
        ingredient.tap()
        XCTAssertEqual(ingredient.value as? String, "Checked")
        screenshot("Mobile recipe")
        app.navigationBars.buttons.firstMatch.tap()
        recipe.swipeLeft()
        app.buttons["Delete"].tap()
        app.buttons["Cancel"].tap()
        XCTAssertTrue(recipe.exists)
        recipe.swipeLeft()
        app.buttons["Delete"].tap()
        app.buttons["Delete recipe"].tap()
        XCTAssertTrue(app.staticTexts["Your recipe collection starts here"].waitForExistence(timeout: 10))
        XCTAssertFalse(recipe.exists)
        app.tabBars.buttons["Add & activity"].tap()
        app.tabBars.buttons["Recipes"].tap()
        XCTAssertTrue(app.staticTexts["Your recipe collection starts here"].waitForExistence(timeout: 10))
    }

    func testRecipeSupportsAccessibilityTextSize() {
        let app = XCUIApplication()
        app.launchArguments = ["--demo", "-UIPreferredContentSizeCategoryName", "UICTContentSizeCategoryAccessibilityXXXL"]
        launch(app)
        app.tabBars.buttons["Recipes"].tap()
        let recipe = app.buttons["library-recipe-recipe_salad"]
        XCTAssertTrue(recipe.waitForExistence(timeout: 10)); recipe.tap()
        XCTAssertTrue(app.staticTexts["recipe-title"].waitForExistence(timeout: 10))
        screenshot("Recipe with accessibility text")
        let lastStep = app.staticTexts.matching(NSPredicate(format: "label CONTAINS %@", "Serve chilled.")).firstMatch
        for _ in 0..<5 {
            if lastStep.isHittable { break }
            app.swipeUp()
        }
        XCTAssertTrue(lastStep.isHittable)
    }

    func testFailedDeleteKeepsRecipe() {
        let app = XCUIApplication()
        app.launchArguments = ["--demo", "--demo-delete-failure"]
        launch(app)
        app.tabBars.buttons["Recipes"].tap()
        let recipe = app.buttons["library-recipe-recipe_salad"]
        XCTAssertTrue(recipe.waitForExistence(timeout: 10))
        recipe.swipeLeft()
        app.buttons["Delete"].tap()
        app.buttons["Delete recipe"].tap()
        XCTAssertTrue(app.alerts["Couldn’t delete recipe"].waitForExistence(timeout: 10))
        app.buttons["OK"].tap()
        XCTAssertTrue(recipe.exists)
    }

    func testSharedURLUsesAutomaticDelivery() {
        let app = XCUIApplication()
        app.launchArguments = ["--demo", "--demo-url-share"]
        launch(app)
        openLatestShare(app)
        XCTAssertTrue(app.staticTexts["Selected for the reader’s next refresh."].waitForExistence(timeout: 20))
        XCTAssertFalse(app.buttons["Send to reader"].exists)
        screenshot("URL delivery")
    }
    func testPastedRecipeLinkSendsAutomatically() {
        let app = XCUIApplication()
        app.launchArguments = ["--demo"]
        launch(app)
        let paste = app.buttons["Paste recipe link"]
        XCTAssertTrue(paste.waitForExistence(timeout: 10)); paste.tap()
        let field = app.textFields["Recipe URL"]
        XCTAssertTrue(field.waitForExistence(timeout: 5))
        field.tap(); field.typeText("https://example.com/recipe#method")
        app.buttons["Share recipe"].tap()
        openLatestShare(app)
        XCTAssertTrue(app.staticTexts["Selected for the reader’s next refresh."].waitForExistence(timeout: 20))
    }
    func testSharedImageUsesAutomaticDelivery() {
        let app = XCUIApplication()
        app.launchArguments = ["--demo", "--demo-image-share"]
        launch(app)
        openLatestShare(app)
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
        launch(app)
        XCTAssertTrue(app.staticTexts["Demo — no server or reader is contacted"].waitForExistence(timeout: 10))
        screenshot("Home")
        app.buttons["Paste recipe text"].tap()
        let editor = app.textViews["Recipe text"]
        XCTAssertTrue(editor.waitForExistence(timeout: 5))
        editor.tap(); editor.typeText("Lemon chickpea salad. Mix chickpeas with lemon juice. Serve chilled.")
        app.buttons["Share recipe"].tap()
        openLatestShare(app)
        XCTAssertTrue(app.staticTexts["Selected for the reader’s next refresh."].waitForExistence(timeout: 15))
        XCTAssertFalse(app.buttons["Send to reader"].exists)
        screenshot("Selected")
    }
    func testSharedInputSendsWithoutAnyButtonPress() {
        let app = XCUIApplication()
        app.launchArguments = ["--demo", "--demo-share"]
        launch(app)
        // No taps between launch and upload dismissal; open history to inspect delivery.
        openLatestShare(app)
        XCTAssertTrue(app.staticTexts["Selected for the reader’s next refresh."].waitForExistence(timeout: 20))
        XCTAssertFalse(app.buttons["Import & preview"].exists)
        XCTAssertFalse(app.buttons["Send to reader"].exists)
        screenshot("Automatic delivery")
    }

    private func openLatestShare(_ app: XCUIApplication) {
        XCTAssertTrue(app.navigationBars["Share to eRecipe"].waitForNonExistence(timeout: 15))
        XCTAssertTrue(app.buttons["Paste recipe text"].waitForExistence(timeout: 15))
        let share = app.buttons.matching(identifier: "recent-share").firstMatch
        XCTAssertTrue(share.waitForExistence(timeout: 10))
        share.tap()
        XCTAssertFalse(app.buttons["Refresh status"].exists)
    }

    func testUploadDismissesWhileProcessingIsStillQueued() {
        let app = XCUIApplication()
        app.launchArguments = ["--demo", "--demo-share", "--demo-processing-queued"]
        launch(app)
        openLatestShare(app)
        XCTAssertTrue(app.staticTexts["Preparing recipe…"].waitForExistence(timeout: 5))
        XCTAssertFalse(app.staticTexts["Selected for the reader’s next refresh."].exists)
    }

    func testUploadFailureStaysOpenAndCanRetry() {
        let app = XCUIApplication()
        app.launchArguments = ["--demo", "--demo-share", "--demo-upload-failure", "--demo-processing-queued"]
        launch(app)
        XCTAssertTrue(app.staticTexts["Upload needs attention"].waitForExistence(timeout: 10))
        XCTAssertTrue(app.buttons["Retry upload"].exists)
        XCTAssertFalse(app.buttons["Dismiss"].exists)
        screenshot("Upload retry")
        app.buttons["Retry upload"].tap()
        openLatestShare(app)
        XCTAssertTrue(app.staticTexts["Preparing recipe…"].waitForExistence(timeout: 5))
    }

    func testConnectionRecoversWithoutRefreshButton() {
        let app = XCUIApplication()
        app.launchArguments = ["--demo", "--demo-share", "--demo-status-failure", "--demo-processing-queued"]
        launch(app)
        XCTAssertTrue(app.staticTexts["Connecting to server…"].waitForExistence(timeout: 5))
        XCTAssertFalse(app.buttons["Check connection"].exists)
        openLatestShare(app)
        XCTAssertTrue(app.staticTexts["Preparing recipe…"].waitForExistence(timeout: 5))
    }
}
