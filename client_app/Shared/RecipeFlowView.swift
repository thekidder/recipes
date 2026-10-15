#if canImport(UIKit)
import SwiftUI

struct RecipeFlowView: View {
    let environment: ClientEnvironment
    let id: String
    @Environment(\.scenePhase) private var scenePhase
    @State private var item: PendingShare?
    @State private var preview: UIImage?
    @State private var page = 1
    @State private var error: String?
    @State private var actionError: String?
    @State private var busy = false
    @State private var deviceMessage: String?
    @State private var loadedAsset: String?

    var body: some View {
        ScrollView {
            VStack(alignment: .leading, spacing: 18) {
                if let item {
                    Text(item.revision?.title ?? "Your recipe").font(.title2.bold())
                    Text(item.summary).font(.subheadline).foregroundStyle(.secondary).lineLimit(3)
                    if item.renderSet == nil {
                        Label(item.message ?? "Preparing recipe…", systemImage: item.phase == "failed" ? "exclamationmark.triangle" : "clock")
                        if item.phase != "failed" { ProgressView().accessibilityLabel("Processing recipe") }
                        Text("Your recipe will be sent automatically when ready. You can close this sheet; background delivery may take longer.").font(.footnote).foregroundStyle(.secondary)
                    }
                    if let render = item.renderSet {
                        if render.pages.count > 1 {
                            Picker("Page", selection: $page) {
                                ForEach(render.pages) { Text("Page \($0.pageNumber)").tag($0.pageNumber) }
                            }
                        }
                        if let preview {
                            Image(uiImage: preview).resizable().scaledToFit().background(.white)
                                .clipShape(RoundedRectangle(cornerRadius: 10)).accessibilityLabel("Rendered recipe, page \(page)")
                        } else { ProgressView("Loading preview…") }
                        ForEach(item.revision?.warnings ?? [], id: \.self) { warning in
                            Label(warning, systemImage: "exclamationmark.triangle").font(.callout)
                        }
                        if item.publication == nil {
                            if item.autoSend == true {
                                Label(item.message ?? "Sending to your reader…", systemImage: "paperplane")
                            } else {
                                Text("This older import was saved for review. Share it again to send it automatically.").font(.footnote)
                            }
                        } else {
                            Label("Selected for the reader’s next refresh.", systemImage: "checkmark.circle.fill").foregroundStyle(.green)
                            if let deviceMessage { Text(deviceMessage).font(.footnote).foregroundStyle(.secondary) }
                        }
                    }
                    if item.phase == "delivery_failed" {
                        Text(item.message ?? "Delivery needs attention.").foregroundStyle(.red)
                        Button("Retry delivery") { retryDelivery() }.disabled(busy)
                    }
                    if item.jobId == nil {
                        Button(item.phase == "failed" ? "Retry upload" : "Resume upload") { upload() }
                            .buttonStyle(.bordered).disabled(busy)
                    } else if item.phase == "failed", item.retryJobId != nil || item.retryCaptureId != nil {
                        Button(item.retryCaptureId != nil ? "Fetch link again" : "Retry processing") { run { try await $0.retry(id) } }.buttonStyle(.bordered).disabled(busy)
                    }
                } else { ProgressView("Loading share…") }
                if let error { Text(error).foregroundStyle(.red).font(.callout) }
                if let actionError { Text(actionError).foregroundStyle(.red).font(.callout) }
            }.padding()
        }
        .navigationTitle("Recipe delivery").navigationBarTitleDisplayMode(.inline)
        .task(id: scenePhase) {
            guard scenePhase == .active else { return }
            while !Task.isCancelled {
                await refresh()
                do { try await Task.sleep(for: .seconds(1)) } catch { return }
            }
        }
        .onChange(of: page) { _, _ in preview = nil; loadedAsset = nil; Task { await refresh() } }
    }
    @MainActor private func refresh() async {
        guard !busy else { return }
        do {
            let connection = try environment.currentConnection()
            let api = try environment.api(for: connection)
            item = try environment.store.item(id)
            if item?.autoSend != true {
                try await Workflow(store: environment.store, api: api, connection: connection).advance(id)
            }
            item = try environment.store.item(id)
            if item?.revision == nil, let render = item?.renderSet {
                let revision: Revision = try await api.get("/api/v1/revisions/\(APIClient.segment(render.revisionId))")
                try environment.store.update(id) { $0.revision = revision }
                item = try environment.store.item(id)
            }
            if let asset = item?.renderSet?.pages.first(where: { $0.pageNumber == page })?.asset, loadedAsset != asset.id {
                let data = try await api.data(asset.href)
                guard let image = UIImage(data: data) else { throw ClientError("The preview image could not be read.") }
                preview = image; loadedAsset = asset.id
            }
            if let receipt = item?.publication {
                let device: Device = try await api.get("/api/v1/device")
                if device.selectedPublication?.id != receipt.publication.id {
                    deviceMessage = "A different recipe is now selected on the server."
                } else if device.selectedAssetFetchedAt != nil {
                    deviceMessage = "The reader fetched this image. Physical display confirmation is unavailable."
                } else { deviceMessage = "Waiting for the reader’s next check-in. A cached image may not produce a new download." }
            }
            error = nil
        } catch is CancellationError {} catch { self.error = error.localizedDescription }
    }
    private func run(_ operation: @escaping (Workflow) async throws -> Void) {
        busy = true
        Task { @MainActor in
            do {
                let connection = try environment.currentConnection()
                try await operation(Workflow(store: environment.store, api: environment.api(for: connection), connection: connection))
                try await environment.uploads.resumeDelivery(id)
                actionError = nil
            } catch { self.actionError = error.localizedDescription }
            busy = false
            item = try? environment.store.item(id)
        }
    }
    private func retryDelivery() {
        busy = true
        Task { @MainActor in
            do { try await environment.uploads.resumeDelivery(id); actionError = nil }
            catch { actionError = error.localizedDescription }
            busy = false
        }
    }
    private func upload() {
        busy = true
        Task { @MainActor in
            do { try await environment.uploads.start(id); actionError = nil } catch { self.actionError = error.localizedDescription }
            busy = false
            item = try? environment.store.item(id)
        }
    }
}
#endif
