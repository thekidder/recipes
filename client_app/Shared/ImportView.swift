#if canImport(UIKit)
import SwiftUI

struct ImportView: View {
    let environment: ClientEnvironment
    let initialInput: ShareInput?
    let finish: () -> Void
    @State private var text = ""
    @State private var status: ServerStatus?
    @State private var error: String?
    @State private var busy = false
    @State private var pendingId: String?
    @State private var initialAttempted = false
    @State private var images: [ImageInput]
    let entryKind: String
    init(environment: ClientEnvironment, initialInput: ShareInput?, entryKind: String = "text", finish: @escaping () -> Void) {
        self.environment = environment; self.initialInput = initialInput; self.finish = finish
        self.entryKind = entryKind
        _images = State(initialValue: initialInput?.images ?? [])
    }
    private var input: ShareInput {
        if initialInput?.kind == "images" { return ShareInput(kind: "images", images: images) }
        if initialInput == nil && entryKind == "url" { return ShareInput(kind: "url", url: text.trimmingCharacters(in: .whitespacesAndNewlines)) }
        return initialInput ?? ShareInput(kind: "text", text: text)
    }

    var body: some View {
        Group {
            if let pendingId { RecipeFlowView(environment: environment, id: pendingId) }
            else {
                Form {
                    Section("Recipe to import") {
                        if initialInput?.kind == "images" {
                            Text(input.summary)
                            Text("Recipe pages are sent in the order shared.").font(.footnote).foregroundStyle(.secondary)
                            ForEach(images.indices, id: \.self) { index in
                                HStack {
                                    if let thumbnail = ImagePreparation.thumbnail(images[index].data) {
                                        Image(uiImage: UIImage(cgImage: thumbnail)).resizable().scaledToFit().frame(width: 90, height: 110)
                                    }
                                    Text("Page \(index + 1)")
                                }
                            }
                        }
                        else if let initialInput { Text(initialInput.summary).lineLimit(8) }
                        else if entryKind == "url" {
                            TextField("https://example.com/recipe", text: $text).keyboardType(.URL).textInputAutocapitalization(.never).autocorrectionDisabled().accessibilityLabel("Recipe URL")
                            Text("Paste a public recipe page. It will be fetched, processed, and sent automatically.").font(.footnote).foregroundStyle(.secondary)
                        }
                        else { TextEditor(text: $text).frame(minHeight: 200).accessibilityLabel("Recipe text") }
                    }
                    if let status {
                        Section {
                            if !status.capabilities.importKinds.contains(input.kind) {
                                Text("This server does not support \(input.kind) imports yet. Share recipe text instead.").foregroundStyle(.orange)
                            } else {
                                if !status.capabilities.publication {
                                    Text("This server has not enabled sending recipes to the reader.").foregroundStyle(.orange)
                                } else if initialInput == nil {
                                    Button("Share recipe") { Task { await submit() } }
                                        .disabled(busy || text.trimmingCharacters(in: .whitespacesAndNewlines).isEmpty)
                                } else {
                                    Text("Your recipe will be sent automatically when ready.")
                                }
                                if !status.readiness.isReady { Text("The server can save your recipe, but processing needs attention in the dashboard.").font(.footnote) }
                            }
                        }
                    } else { Section { Button("Check connection") { Task { await loadStatus() } } } }
                    if busy { ProgressView("Saving recipe…") }
                    if let error { Text(error).foregroundStyle(.red).font(.callout) }
                }
            }
        }
        .navigationTitle(pendingId == nil ? "Share to eRecipe" : "Recipe delivery")
        .toolbar {
            ToolbarItem(placement: .cancellationAction) { Button("Done", action: finish) }
        }
        .task { await loadStatus() }
    }
    @MainActor private func loadStatus() async {
        do {
            let connection = try environment.currentConnection()
            status = try await environment.api(for: connection).status(for: connection)
            error = nil
            if initialInput != nil, !initialAttempted, pendingId == nil,
               status?.capabilities.importKinds.contains(input.kind) == true,
               status?.capabilities.publication == true {
                initialAttempted = true
                await submit()
            }
        } catch { self.error = error.localizedDescription }
    }
    @MainActor private func submit() async {
        guard let status, !busy, pendingId == nil else { return }
        busy = true
        defer { busy = false }
        do {
                let connection = try environment.currentConnection()
                guard status.capabilities.importKinds.contains(input.kind) else { throw ClientError("This input type is not supported by the server.") }
                let item = try environment.store.create(input: input, connection: connection, limits: status.capabilities.limits)
                pendingId = item.id
                do { try await environment.uploads.start(item.id) }
                catch { try environment.store.update(item.id) { $0.phase = $0.jobId == nil ? "failed" : "delivery_failed"; $0.message = error.localizedDescription } }
            } catch { self.error = error.localizedDescription }
    }
}
#endif
