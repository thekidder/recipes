#if canImport(UIKit)
import SwiftUI

struct RecipeDetailView: View {
    let environment: ClientEnvironment
    let recipeID: String
    @Environment(\.scenePhase) private var scenePhase
    @State private var recipe: RecipeSummary?
    @State private var error: String?

    var body: some View {
        ScrollView {
            VStack(alignment: .leading, spacing: 24) {
                if let recipe {
                    if recipe.archivedAt != nil {
                        Label("Deleted from the library", systemImage: "archivebox").foregroundStyle(.secondary)
                    }
                    if let revision = recipe.currentRevision, let markdown = revision.markdown {
                        FormattedRecipeView(title: revision.title, document: RecipeDocument(markdown: markdown))
                        ForEach(Array(revision.warnings.enumerated()), id: \.offset) { _, warning in
                            Label(warning, systemImage: "exclamationmark.triangle").font(.callout).foregroundStyle(.secondary)
                        }
                    } else {
                        ContentUnavailableView(recipe.statusLabel, systemImage: "text.book.closed",
                            description: Text(recipe.processing.failedJobCount > 0
                                ? "Recipe processing needs attention. If you shared it from this device, open Add & activity to retry."
                                : "The formatted recipe will appear here when processing finishes."))
                    }
                } else if error == nil { ProgressView("Loading recipe…").frame(maxWidth: .infinity) }
                if let error {
                    Label(error, systemImage: "wifi.exclamationmark").foregroundStyle(.red)
                    Button("Try again") { Task { await refresh() } }.buttonStyle(.bordered)
                }
            }
            .frame(maxWidth: 680, alignment: .leading)
            .padding(20)
            .frame(maxWidth: .infinity)
        }
        .background(Color(uiColor: .systemGroupedBackground))
        .navigationTitle("Recipe")
        .navigationBarTitleDisplayMode(.inline)
        .refreshable { await refresh() }
        .task(id: scenePhase) {
            guard scenePhase == .active else { return }
            await refresh()
            while !Task.isCancelled, recipe?.currentRevision == nil, error == nil {
                do { try await Task.sleep(for: .seconds(3)) } catch { return }
                await refresh()
            }
        }
    }
    @MainActor private func refresh() async {
        do {
            let connection = try environment.currentConnection()
            let fetched = try await environment.api(for: connection).recipe(recipeID)
            guard try environment.currentConnection().context == connection.context else { return }
            recipe = fetched; error = nil
        } catch is CancellationError {} catch { self.error = error.localizedDescription }
    }
}

struct FormattedRecipeView: View {
    let title: String
    let document: RecipeDocument
    @State private var checkedIngredients = Set<Int>()
    private let details = [("servings", "Servings"), ("prep_time", "Prep"), ("cook_time", "Cook"), ("total_time", "Total"), ("other_time", "Other time")]

    var body: some View {
        VStack(alignment: .leading, spacing: 18) {
            Text(title).font(.largeTitle.bold()).fixedSize(horizontal: false, vertical: true)
                .accessibilityAddTraits(.isHeader).accessibilityIdentifier("recipe-title")
            if details.contains(where: { document.metadata[$0.0] != nil }) {
                ViewThatFits(in: .horizontal) {
                    HStack(alignment: .top, spacing: 20) { metadata }
                    VStack(alignment: .leading, spacing: 12) { metadata }
                }
                .padding(16).frame(maxWidth: .infinity, alignment: .leading)
                .background(Color(uiColor: .secondarySystemGroupedBackground), in: RoundedRectangle(cornerRadius: 16))
            }
            ForEach(document.blocks) { block in
                switch block.kind {
                case .heading:
                    Text(inline(block.text)).font(.title2.bold()).padding(.top, 12).accessibilityAddTraits(.isHeader)
                case .group:
                    Text(inline(block.text)).font(.headline).padding(.top, 6).accessibilityAddTraits(.isHeader)
                case .ingredient:
                    Button {
                        if !checkedIngredients.insert(block.id).inserted { checkedIngredients.remove(block.id) }
                    } label: {
                        HStack(alignment: .top, spacing: 12) {
                            Image(systemName: checkedIngredients.contains(block.id) ? "checkmark.circle.fill" : "circle")
                                .font(.title3).foregroundStyle(.tint).padding(.top, 2)
                            Text(inline(block.text)).strikethrough(checkedIngredients.contains(block.id))
                                .foregroundStyle(checkedIngredients.contains(block.id) ? .secondary : .primary)
                                .frame(maxWidth: .infinity, alignment: .leading)
                        }
                        .padding(.vertical, 10).contentShape(Rectangle())
                    }
                    .buttonStyle(.plain)
                    .accessibilityValue(checkedIngredients.contains(block.id) ? "Checked" : "Unchecked")
                    .accessibilityHint("Double tap to check off this ingredient")
                case .step(let number):
                    HStack(alignment: .top, spacing: 14) {
                        Text("\(number)").font(.headline).foregroundStyle(.tint)
                            .frame(minWidth: 32, minHeight: 32)
                            .background(.tint.opacity(0.1), in: Circle())
                        Text(inline(block.text)).frame(maxWidth: .infinity, alignment: .leading).padding(.top, 4)
                    }
                    .padding(.vertical, 6).accessibilityElement(children: .combine)
                case .note:
                    Text(inline(block.text)).foregroundStyle(.secondary)
                }
            }
            if let footer = document.metadata["footer"] {
                Divider()
                Text(footer).font(.footnote).foregroundStyle(.secondary)
            }
        }
        .font(.body).lineSpacing(5).textSelection(.enabled)
    }
    private var metadata: some View {
        ForEach(details, id: \.0) { key, label in
            if let value = document.metadata[key] {
                VStack(alignment: .leading, spacing: 4) {
                    Text(label).font(.caption).foregroundStyle(.secondary)
                    Text(value).font(.subheadline.weight(.semibold))
                }.fixedSize(horizontal: false, vertical: true)
            }
        }
    }
    private func inline(_ text: String) -> AttributedString {
        (try? AttributedString(markdown: text, options: .init(interpretedSyntax: .inlineOnlyPreservingWhitespace))) ?? AttributedString(text)
    }
}
#endif
