import Foundation

struct RecipeSummary: Decodable, Identifiable {
    let id: String
    let title: String?
    let lockVersion: Int
    let createdBy: Person
    let createdAt: String
    let archivedAt: String?
    let currentRevision: Revision?
    let processing: RecipeProcessing
    var displayTitle: String { title ?? "Recipe being prepared" }
    var statusLabel: String {
        if processing.activeJobCount > 0 { return "Preparing" }
        if title != nil { return "Ready to read" }
        if processing.failedJobCount > 0 { return "Needs attention" }
        return "Waiting for recipe"
    }
}
struct RecipeProcessing: Decodable {
    let activeJobCount: Int
    let failedJobCount: Int
}
struct RecipePage: Decodable {
    let items: [RecipeSummary]
    let nextCursor: String?
    let hasMore: Bool
}
struct ArchiveReceipt: Decodable { let recipeId: String; let archivedAt: String }

extension APIClient {
    /// Fetch every page before replacing the displayed library, so a partial failure
    /// cannot make older recipes disappear or exclude them from search.
    func recipes() async throws -> [RecipeSummary] {
        var result: [RecipeSummary] = []
        var cursors = Set<String>()
        var cursor: String?
        repeat {
            try Task.checkCancellation()
            var components = URLComponents()
            components.path = "/api/v1/recipes"
            components.queryItems = [URLQueryItem(name: "visibility", value: "active"), URLQueryItem(name: "limit", value: "100")]
            if let cursor { components.queryItems?.append(URLQueryItem(name: "cursor", value: cursor)) }
            components.percentEncodedQuery = components.percentEncodedQuery?.replacingOccurrences(of: "+", with: "%2B")
            let page: RecipePage = try await get(components.string!)
            result.append(contentsOf: page.items)
            guard page.hasMore else { break }
            guard let next = page.nextCursor, cursors.insert(next).inserted else {
                throw ClientError("The recipe list could not be fully loaded. Pull to refresh and try again.")
            }
            cursor = next
        } while true
        var seen = Set<String>()
        return result.filter { $0.archivedAt == nil && seen.insert($0.id).inserted }
    }
    func recipe(_ id: String) async throws -> RecipeSummary {
        try await get("/api/v1/recipes/\(Self.segment(id))")
    }
    func archive(_ recipe: RecipeSummary, key: String) async throws {
        let _: ArchiveReceipt = try await post("/api/v1/recipes/\(Self.segment(recipe.id))/archive",
            body: ["expected_lock_version": recipe.lockVersion], key: key)
    }
}
