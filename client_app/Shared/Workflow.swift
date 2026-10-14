import Foundation

struct Workflow {
    let store: SharedStore
    let api: APIClient
    let connection: Connection

    func advance(_ id: String) async throws {
        guard let item = try store.item(id), item.context == connection.context, let rootJob = item.jobId,
              item.renderSet == nil, item.publication == nil else { return }
        var queue = [rootJob], seen = Set<String>()
        var renderId: String?
        while !queue.isEmpty {
            try Task.checkCancellation()
            let jobId = queue.removeFirst()
            guard seen.insert(jobId).inserted, seen.count <= 30 else { throw ClientError("Unexpected processing job chain.") }
            let job: Job = try await api.get("/api/v1/jobs/\(APIClient.segment(jobId))")
            if job.status == "failed" || job.status == "cancelled" {
                try store.update(id) {
                    $0.phase = "failed"; $0.message = job.error?.message ?? "Processing was cancelled."
                    $0.retryJobId = job.retryAllowed && ["extract", "render"].contains(job.kind) ? job.id : nil
                    $0.retryCaptureId = job.kind == "capture" && job.status == "failed" ? job.sourceCaptureId : nil
                }
                return
            }
            guard ["queued", "running", "succeeded"].contains(job.status) else { throw ClientError("The server returned an unsupported job state.") }
            if let result = job.result.renderSetId { renderId = result }
            queue.append(contentsOf: job.downstreamJobIds)
        }
        guard let renderId else {
            try store.update(id) { $0.phase = "processing"; $0.message = "Extracting and rendering your recipe…" }
            return
        }
        let render: RenderSet = try await api.get("/api/v1/render-sets/\(APIClient.segment(renderId))")
        guard !render.pages.isEmpty, render.pageCount == render.pages.count,
              render.pages.map(\.pageNumber) == Array(1...render.pageCount),
              render.pages.allSatisfy({ $0.asset.mediaType == "image/png" }) else { throw ClientError("The server returned an incomplete preview.") }
        let revision: Revision = try await api.get("/api/v1/revisions/\(APIClient.segment(render.revisionId))")
        try store.update(id) { $0.renderSet = render; $0.revision = revision; $0.phase = "ready"; $0.message = nil }
    }

    func retry(_ id: String) async throws {
        let item = try store.update(id) { value in
            guard value.retryJobId != nil || value.retryCaptureId != nil else { throw ClientError("This job cannot be retried. Check the server dashboard.") }
            if value.retryKey == nil { value.retryKey = UUID().uuidString }
        }
        var newJobId: String?
        if let capture = item.retryCaptureId, let recipe = item.recipeId {
            let receipt: ImportReceipt = try await api.post("/api/v1/recipes/\(APIClient.segment(recipe))/refresh",
                body: ["source_capture_id": capture], key: item.retryKey)
            guard receipt.recipeId == recipe else { throw ClientError("The server returned a different recipe.") }
            newJobId = receipt.jobId
        } else {
            guard let job = item.retryJobId else { throw ClientError("The recipe reference is missing. Share the URL again.") }
            let _: Job = try await api.post("/api/v1/jobs/\(APIClient.segment(job))/retry", key: item.retryKey)
        }
        try store.update(id) {
            if let newJobId { $0.jobId = newJobId }
            $0.phase = "processing"; $0.message = nil; $0.retryKey = nil; $0.retryJobId = nil; $0.retryCaptureId = nil
        }
    }

    func publish(_ id: String, page: Int) async throws {
        let status = try await api.status(for: connection)
        guard status.capabilities.publication else { throw ClientError("This server has not enabled sending to the reader.") }
        let item = try store.update(id) { value in
            guard let render = value.renderSet, render.pages.contains(where: { $0.pageNumber == page }) else { throw ClientError("Wait for a complete preview before sending.") }
            guard value.publication == nil else { throw ClientError("This send is already confirmed.") }
            if let previous = value.publishPage, previous != page { throw ClientError("Resolve the pending send before selecting another page.") }
            if value.publishKey == nil { value.publishKey = UUID().uuidString; value.publishPage = page }
        }
        let receipt: PublicationReceipt = try await api.post("/api/v1/publications",
            body: ["render_set_id": item.renderSet!.id, "page_number": page], key: item.publishKey)
        try store.update(id) { $0.publication = receipt; $0.phase = "selected"; $0.message = "Selected for the reader’s next refresh." }
    }
}
