import Foundation

// Each step is serializable into URLSession.taskDescription so the app can handle
// extension transfers after process termination, without a live SwiftUI view.
struct DeliveryTransfer: Codable {
    enum Step: Codable, Equatable {
        case status
        case job(String, remaining: [String], visited: [String])
        case render(String)
        case publication
    }
    let itemId: String
    var step: Step
    var delay: TimeInterval = 0
    var failures = 0
    var description: String { "delivery:" + (try! JSONEncoder().encode(self)).base64EncodedString() }
    init(itemId: String, step: Step) { self.itemId = itemId; self.step = step }
    init?(description: String?) {
        guard let description, description.hasPrefix("delivery:"),
              let data = Data(base64Encoded: String(description.dropFirst(9))),
              let value = try? JSONDecoder().decode(Self.self, from: data) else { return nil }
        self = value
    }
    var path: String {
        get throws {
            switch step {
            case .status: return "/api/v1/status"
            case .job(let id, _, _): return "/api/v1/jobs/\(try APIClient.segment(id))"
            case .render(let id): return "/api/v1/render-sets/\(try APIClient.segment(id))"
            case .publication: return "/api/v1/publications"
            }
        }
    }
}

struct AutomaticDelivery {
    let store: SharedStore
    let connection: Connection

    func eligible(_ id: String) throws -> PendingShare? {
        guard let item = try store.item(id), item.context == connection.context,
              try store.connection()?.context == connection.context,
              item.autoSend == true, item.publication == nil, item.jobId != nil else { return nil }
        return item
    }
    func initial(_ id: String) throws -> DeliveryTransfer? {
        guard try eligible(id) != nil else { return nil }
        return DeliveryTransfer(itemId: id, step: .status)
    }
    func publicationBody(_ id: String) throws -> (Data, String) {
        let item = try store.update(id) { value in
            guard value.autoSend == true, value.publication == nil, let render = value.renderSet,
                  render.pages.contains(where: { $0.pageNumber == 1 }) else { throw ClientError("The recipe is not ready for automatic delivery.") }
            if value.publishKey == nil { value.publishKey = UUID().uuidString; value.publishPage = 1 }
            guard value.publishPage == 1 else { throw ClientError("A different page already has a pending send.") }
            value.phase = "sending"; value.message = "Sending to your reader…"
        }
        return (try JSONSerialization.data(withJSONObject: ["render_set_id": item.renderSet!.id, "page_number": 1]), item.publishKey!)
    }
    func consume(_ transfer: DeliveryTransfer, data: Data) throws -> DeliveryTransfer? {
        guard let item = try eligible(transfer.itemId) else { return nil }
        let id = item.id
        switch transfer.step {
        case .status:
            let status = try Wire.decoder.decode(ServerStatus.self, from: data)
            guard status.apiVersion == "1", status.serverId == connection.serverId, status.user.id == connection.user.id else { throw ClientError("Server identity changed. Register again before sending recipes.") }
            guard status.capabilities.publication else { throw ClientError("This server has not enabled delivery to the reader.") }
            return DeliveryTransfer(itemId: id, step: item.renderSet == nil ? .job(item.jobId!, remaining: [], visited: []) : .publication)
        case .job(let expectedId, let remaining, let visited):
            let job = try Wire.decoder.decode(Job.self, from: data)
            guard job.id == expectedId else { throw ClientError("The server returned a different processing job.") }
            if job.status == "failed" || job.status == "cancelled" {
                try store.update(id) {
                    guard $0.publication == nil else { return }
                    $0.phase = "failed"; $0.message = job.error?.message ?? "Processing was cancelled."
                    $0.retryJobId = job.retryAllowed && ["extract", "render"].contains(job.kind) ? job.id : nil
                    $0.retryCaptureId = job.kind == "capture" && job.status == "failed" ? job.sourceCaptureId : nil
                }
                return nil
            }
            if ["queued", "running"].contains(job.status) {
                var next = transfer; next.delay = min(10, max(2, transfer.delay + 2)); next.failures = 0
                return next
            }
            guard job.status == "succeeded" else { throw ClientError("Unsupported processing job state.") }
            if let renderId = job.result.renderSetId { return DeliveryTransfer(itemId: id, step: .render(renderId)) }
            var queue = remaining + job.downstreamJobIds
            guard !queue.isEmpty else { throw ClientError("Processing completed without a rendered recipe.") }
            let nextId = queue.removeFirst(), seen = visited + [job.id]
            guard seen.count < 30, !seen.contains(nextId) else { throw ClientError("Unexpected processing job chain.") }
            return DeliveryTransfer(itemId: id, step: .job(nextId, remaining: queue, visited: seen))
        case .render(let expectedId):
            let render = try Wire.decoder.decode(RenderSet.self, from: data)
            guard render.id == expectedId, !render.pages.isEmpty, render.pageCount == render.pages.count,
                  render.pages.map(\.pageNumber) == Array(1...render.pageCount),
                  render.pages.allSatisfy({ $0.asset.mediaType == "image/png" }) else { throw ClientError("The server returned an incomplete render set.") }
            try store.update(id) { guard $0.publication == nil else { return }; $0.renderSet = render; $0.phase = "sending"; $0.message = "Sending to your reader…" }
            // Recheck identity and publication availability immediately before publication.
            return DeliveryTransfer(itemId: id, step: .status)
        case .publication:
            let receipt = try Wire.decoder.decode(PublicationReceipt.self, from: data)
            guard receipt.status == "selected", receipt.publication.renderSetId == item.renderSet?.id,
                  receipt.publication.pageNumber == 1 else { throw ClientError("The server returned an unexpected delivery receipt.") }
            try store.update(id) { $0.publication = receipt; $0.phase = "selected"; $0.message = "Selected for the reader’s next refresh." }
            return nil
        }
    }
}
