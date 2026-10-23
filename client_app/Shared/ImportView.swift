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
            if let pendingId { ShareUploadView(environment: environment, id: pendingId, finish: finish) }
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
                            Text("Paste a public recipe page. If the site blocks downloads, share a Reader PDF from Safari instead.").font(.footnote).foregroundStyle(.secondary)
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
                    } else { Section { ProgressView("Connecting to server…") } }
                    if busy { ProgressView("Saving recipe…") }
                    if let error {
                        Text(error).foregroundStyle(.red).font(.callout)
                        if initialAttempted { Button("Retry share") { Task { await submit() } }.disabled(busy || status == nil) }
                    }
                }
            }
        }
        .navigationTitle("Share to eRecipe")
        .toolbar {
            ToolbarItem(placement: .cancellationAction) { Button("Close", action: finish) }
        }
        .task {
            while !Task.isCancelled && pendingId == nil {
                await loadStatus()
                do { try await Task.sleep(for: .seconds(2)) } catch { return }
            }
        }
    }
    @MainActor private func loadStatus() async {
        do {
            let connection = try environment.currentConnection()
            status = try await environment.api(for: connection).status(for: connection)
            if !initialAttempted { error = nil }
            if initialInput != nil, !initialAttempted, pendingId == nil,
               status?.capabilities.importKinds.contains(input.kind) == true,
               status?.capabilities.publication == true {
                initialAttempted = true
                await submit()
            }
        } catch is CancellationError {} catch { status = nil; self.error = error.localizedDescription }
    }
    @MainActor private func submit() async {
        guard let status, !busy, pendingId == nil else { return }
        busy = true
        error = nil
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

// The share sheet only tracks upload acceptance. Processing and publication
// continue through UploadManager, independently of this view's lifetime.
private struct ShareUploadView: View {
    let environment: ClientEnvironment
    let id: String
    let finish: () -> Void
    @State private var item: PendingShare?
    @State private var error: String?
    @State private var retrying = false
    @State private var finished = false

    var body: some View {
        VStack(spacing: 24) {
            Spacer()
            if item?.uploadCompletedAt != nil {
                Image(systemName: "checkmark.circle.fill").font(.system(size: 56)).foregroundStyle(.green)
                Text("Recipe uploaded").font(.title2.bold())
                Text("You can follow its progress in eRecipe.").foregroundStyle(.secondary)
                Button(action: finish) { Text("Dismiss").frame(maxWidth: .infinity).padding(.vertical, 8) }
                    .buttonStyle(.borderedProminent).controlSize(.large)
            } else if item?.phase == "failed" || error != nil {
                Image(systemName: "exclamationmark.triangle").font(.largeTitle).foregroundStyle(.orange)
                Text("Upload needs attention").font(.title2.bold())
                Text(error ?? item?.message ?? "Please try again.").foregroundStyle(.secondary)
                Button("Retry upload") { retry() }.buttonStyle(.borderedProminent).controlSize(.large).disabled(retrying)
            } else {
                ProgressView().controlSize(.large)
                Text("Uploading recipe…").font(.title2.bold())
                Text("This sheet closes automatically once your recipe is saved on the server.").foregroundStyle(.secondary)
            }
            if let item { Text(item.summary).font(.footnote).foregroundStyle(.secondary).lineLimit(3) }
            Spacer()
        }
        .padding(28).frame(maxWidth: .infinity).multilineTextAlignment(.center)
        .task {
            while !Task.isCancelled {
                do {
                    item = try environment.store.item(id)
                    if item?.uploadCompletedAt != nil, !finished {
                        finished = true
                        finish()
                        return
                    }
                } catch { self.error = error.localizedDescription }
                // Local receipt checks only: no network polling during an upload.
                do { try await Task.sleep(for: .milliseconds(250)) } catch { return }
            }
        }
    }

    private func retry() {
        retrying = true
        error = nil
        Task { @MainActor in
            do { try await environment.uploads.start(id) }
            catch { self.error = error.localizedDescription }
            item = try? environment.store.item(id)
            retrying = false
        }
    }
}
#endif
