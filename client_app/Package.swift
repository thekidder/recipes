// swift-tools-version: 5.9
import PackageDescription

let package = Package(
    name: "ERecipeCore",
    platforms: [.macOS(.v13), .iOS(.v17)],
    products: [.library(name: "ERecipeCore", targets: ["ERecipeCore"])],
    targets: [
        .target(name: "ERecipeCore", path: "Shared", linkerSettings: [.linkedLibrary("sqlite3")]),
        .testTarget(name: "ERecipeCoreTests", dependencies: ["ERecipeCore"], path: "Tests", resources: [.copy("Fixtures")])
    ]
)
