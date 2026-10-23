import SwiftUI

struct LibraryView: View {
    let environment: ClientEnvironment
    @Environment(\.scenePhase) private var scenePhase
    @State private var recipes: [RecipeSummary] = []
    @State private var query = ""
    @State private var alphabetical = false
    @State private var loading = false
    @State private var loaded = false
    @State private var connected = false
    @State private var error: String?
    @State private var deleteError: String?
    @State private var pendingDelete: RecipeSummary?
    @State private var deleting = false
    @State private var deletionKeys: [String: String] = [:]
    @State private var context: String?
    @State private var generation = UUID()

    private var visibleRecipes: [RecipeSummary] {
        let term = query.trimmingCharacters(in: .whitespacesAndNewlines)
        let matches = recipes.filter { term.isEmpty || $0.displayTitle.localizedStandardContains(term) || $0.createdBy.displayName.localizedStandardContains(term) }
        return matches.sorted {
            if alphabetical { return $0.displayTitle.localizedStandardCompare($1.displayTitle) == .orderedAscending }
            return $0.createdAt == $1.createdAt ? $0.id > $1.id : $0.createdAt > $1.createdAt
        }
    }

    var body: some View {
        NavigationStack {
            List {
                if environment.demo {
                    Label("Demo recipe library", systemImage: "testtube.2").font(.footnote).foregroundStyle(.orange)
                }
                if let error {
                    Section {
                        Label(error, systemImage: "wifi.exclamationmark").font(.callout).foregroundStyle(.red)
                        Button("Try again") { Task { await refresh() } }
                    }
                }
                if !connected && loaded {
                    ContentUnavailableView("Connect to your library", systemImage: "books.vertical",
                        description: Text("Open Add & activity to connect using your server’s registration QR code."))
                } else if loading && !loaded {
                    ProgressView("Loading recipes…")
                } else if loaded && recipes.isEmpty && error == nil {
                    ContentUnavailableView("Your recipe collection starts here", systemImage: "book.closed",
                        description: Text("Use Add & activity to paste a recipe or choose photos, or share a recipe from another app."))
                } else if loaded && visibleRecipes.isEmpty && !query.isEmpty {
                    ContentUnavailableView.search(text: query)
                } else {
                    Section {
                        ForEach(visibleRecipes) { recipe in
                            NavigationLink {
                                RecipeDetailView(environment: environment, recipeID: recipe.id)
                            } label: { row(recipe) }
                            .accessibilityIdentifier("library-recipe-\(recipe.id)")
                            .swipeActions(edge: .trailing, allowsFullSwipe: false) {
                                Button(role: .destructive) { pendingDelete = recipe } label: { Label("Delete", systemImage: "trash") }
                            }
                            .contextMenu {
                                Button(role: .destructive) { pendingDelete = recipe } label: { Label("Delete recipe", systemImage: "trash") }
                            }
                            .accessibilityAction(named: "Delete recipe") { pendingDelete = recipe }
                        }
                    } header: {
                        Text("\(visibleRecipes.count) recipe\(visibleRecipes.count == 1 ? "" : "s")")
                    } footer: {
                        if !recipes.isEmpty { Text("Recipes shared by everyone connected to this server. Swipe left on a recipe to delete it.") }
                    }
                }
            }
            .navigationTitle("Recipes")
            .searchable(text: $query, placement: .navigationBarDrawer(displayMode: .always), prompt: "Search recipes or people")
            .toolbar {
                ToolbarItem(placement: .topBarTrailing) {
                    Menu {
                        Picker("Sort recipes", selection: $alphabetical) {
                            Text("Newest first").tag(false)
                            Text("Title A–Z").tag(true)
                        }
                    } label: { Label("Sort recipes", systemImage: "arrow.up.arrow.down") }
                }
            }
            .refreshable { await refresh() }
            .alert("Delete \(pendingDelete?.displayTitle ?? "recipe")?", isPresented: Binding(
                get: { pendingDelete != nil }, set: { if !$0 { pendingDelete = nil } })) {
                if let recipe = pendingDelete {
                    Button("Delete recipe", role: .destructive) { Task { await delete(recipe) } }
                }
                Button("Cancel", role: .cancel) { pendingDelete = nil }
            } message: {
                Text("This removes the recipe from everyone’s library. Its history and any recipe already selected on the reader are retained.")
            }
            .alert("Couldn’t delete recipe", isPresented: Binding(get: { deleteError != nil }, set: { if !$0 { deleteError = nil } })) {
                Button("OK", role: .cancel) { deleteError = nil }
            } message: { Text(deleteError ?? "") }
            .overlay { if deleting { ProgressView("Deleting recipe…").padding().background(.regularMaterial, in: RoundedRectangle(cornerRadius: 14)) } }
            .disabled(deleting)
            .task(id: scenePhase) {
                guard scenePhase == .active else { return }
                await refresh()
            }
        }
    }
    private func row(_ recipe: RecipeSummary) -> some View {
        HStack(alignment: .top, spacing: 14) {
            Image(systemName: "book.closed").font(.title2).foregroundStyle(.tint)
                .frame(width: 46, height: 52).background(.tint.opacity(0.08), in: RoundedRectangle(cornerRadius: 12))
            VStack(alignment: .leading, spacing: 7) {
                Text(recipe.displayTitle).font(.headline).lineLimit(3)
                Text("By \(recipe.createdBy.displayName)").font(.subheadline).foregroundStyle(.secondary)
                Label(recipe.statusLabel, systemImage: recipe.processing.activeJobCount > 0 ? "clock" : recipe.title != nil ? "checkmark.circle" : "exclamationmark.circle")
                    .font(.caption).foregroundStyle(.secondary)
            }
        }.padding(.vertical, 8)
    }
    @MainActor private func refresh() async {
        guard !loading && !deleting else { return }
        loading = true
        let requestGeneration = generation
        defer { loading = false }
        do {
            guard let connection = try environment.store.connection() else {
                recipes = []; context = nil; connected = false; loaded = true; error = nil; return
            }
            connected = true
            if context != connection.context { recipes = []; loaded = false; deletionKeys = [:]; context = connection.context }
            let fetched = try await environment.api(for: connection).recipes()
            guard generation == requestGeneration, try environment.store.connection()?.context == connection.context else { return }
            recipes = fetched; loaded = true; error = nil
        } catch is CancellationError {} catch { self.error = error.localizedDescription; loaded = true }
    }
    @MainActor private func delete(_ recipe: RecipeSummary) async {
        deleting = true
        generation = UUID()
        pendingDelete = nil
        do {
            let connection = try environment.currentConnection()
            guard connection.context == context else { throw ClientError("The account changed. Refresh the library before deleting a recipe.") }
            let identity = "\(recipe.id):\(recipe.lockVersion)"
            let key = deletionKeys[identity] ?? UUID().uuidString
            deletionKeys[identity] = key
            try await environment.api(for: connection).archive(recipe, key: key)
            recipes.removeAll { $0.id == recipe.id }
            deletionKeys[identity] = nil
        } catch let problem as APIError where problem.error.code == "recipe_archived" {
            recipes.removeAll { $0.id == recipe.id }
        } catch {
            deleteError = error.localizedDescription + " Pull to refresh before trying again."
        }
        deleting = false
    }
}
