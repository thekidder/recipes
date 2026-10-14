#if canImport(UIKit)
import Foundation
import UIKit

final class UploadManager: NSObject, URLSessionDataDelegate, URLSessionDownloadDelegate, @unchecked Sendable {
    private let environment: ClientEnvironment
    private var sessions: [String: URLSession] = [:]
    private var buffers: [String: Data] = [:]
    private var downloadErrors: [String: Error] = [:]
    private var demoRunning = Set<String>()
    private var completions: [String: () -> Void] = [:]
    private let lock = NSRecursiveLock()
    private let delegateQueue: OperationQueue = {
        let queue = OperationQueue(); queue.maxConcurrentOperationCount = 1; return queue
    }()
    init(environment: ClientEnvironment) { self.environment = environment }
    private var identifier: String { environment.group + (Bundle.main.bundleURL.pathExtension == "appex" ? ".share-upload" : ".app-upload") }
    private func session(_ id: String) -> URLSession {
        lock.lock(); defer { lock.unlock() }
        if let session = sessions[id] { return session }
        let configuration = URLSessionConfiguration.background(withIdentifier: id)
        configuration.sharedContainerIdentifier = environment.group
        configuration.isDiscretionary = false
        configuration.sessionSendsLaunchEvents = true
        configuration.timeoutIntervalForResource = 86_400
        configuration.httpCookieStorage = nil
        configuration.urlCache = nil
        let result = URLSession(configuration: configuration, delegate: self, delegateQueue: delegateQueue)
        sessions[id] = result
        return result
    }
    func reconnect(_ id: String, completion: @escaping () -> Void) {
        guard [environment.group + ".share-upload", environment.group + ".app-upload"].contains(id) else { completion(); return }
        lock.lock(); completions[id] = completion; lock.unlock()
        _ = session(id)
    }
    func start(_ id: String) async throws {
        let connection = try environment.currentConnection()
        guard let item = try environment.store.item(id), item.context == connection.context, item.jobId == nil else { return }
        let api = try environment.api(for: connection)
        if connection.isDemo {
            let data = try Data(contentsOf: environment.store.bodyURL(id))
            let request = try api.request("/api/v1/imports", method: "POST", body: data, key: item.importKey, contentType: item.contentType)
            let demoSession = DemoProtocol.session()
            defer { demoSession.invalidateAndCancel() }
            let (response, _) = try await demoSession.data(for: request)
            try accept(response, id: id)
            try await resumeDelivery(id)
            return
        }
        let current = session(identifier)
        let tasks = await current.allTasks
        guard !tasks.contains(where: { $0.taskDescription == id }) else { return }
        // Persist before starting. A crash after creation is recovered with the original idempotency key.
        try environment.store.update(id) { $0.phase = "uploading"; $0.message = "Uploading recipe…" }
        let request = try api.request("/api/v1/imports", method: "POST", key: item.importKey, contentType: item.contentType)
        let task = current.uploadTask(with: request, fromFile: environment.store.bodyURL(id))
        task.taskDescription = id
        task.resume()
    }
    func resumePending() async {
        guard let connection = try? environment.currentConnection(),
              let items = try? environment.store.items(context: connection.context) else { return }
        for item in items where item.autoSend == true && item.publication == nil && !["failed", "delivery_failed"].contains(item.phase) {
            do {
                if item.jobId == nil { try await start(item.id) }
                else { try await resumeDelivery(item.id) }
            } catch { _ = try? environment.store.update(item.id) { $0.message = error.localizedDescription } }
        }
    }
    private func claimDemo(_ id: String) -> Bool {
        lock.lock(); defer { lock.unlock() }; return demoRunning.insert(id).inserted
    }
    private func finishDemo(_ id: String) { lock.lock(); demoRunning.remove(id); lock.unlock() }
    func resumeDelivery(_ id: String) async throws {
        let connection = try environment.currentConnection()
        let delivery = AutomaticDelivery(store: environment.store, connection: connection)
        guard let first = try delivery.initial(id) else { return }
        if connection.isDemo {
            guard claimDemo(id) else { return }
            defer { finishDemo(id) }
            let api = try environment.api(for: connection)
            var next: DeliveryTransfer? = first
            while let step = next {
                try Task.checkCancellation()
                let data: Data
                if step.step == .publication {
                    let (body, key) = try delivery.publicationBody(id)
                    data = try await api.data(step.path, method: "POST", body: body, key: key)
                } else { data = try await api.data(step.path) }
                next = try delivery.consume(step, data: data)
                if let delay = next?.delay, delay > 0 { try await Task.sleep(nanoseconds: UInt64(delay * 1_000_000_000)) }
            }
            return
        }
        let current = session(identifier)
        guard !(await current.allTasks).contains(where: { DeliveryTransfer(description: $0.taskDescription)?.itemId == id }) else { return }
        try enqueue(first, session: current)
    }
    private func enqueue(_ transfer: DeliveryTransfer, session: URLSession) throws {
        let connection = try environment.currentConnection()
        let delivery = AutomaticDelivery(store: environment.store, connection: connection)
        guard try delivery.eligible(transfer.itemId) != nil else { return }
        let api = try environment.api(for: connection)
        let task: URLSessionTask
        if transfer.step == .publication {
            let (body, key) = try delivery.publicationBody(transfer.itemId)
            // Stable filename and key make a lost publication response safe to replay.
            let file = environment.store.root.appendingPathComponent(transfer.itemId + ".publication")
            try body.write(to: file, options: .atomic)
            task = try session.uploadTask(with: api.request(transfer.path, method: "POST", key: key), fromFile: file)
        } else {
            task = try session.downloadTask(with: api.request(transfer.path))
        }
        task.taskDescription = transfer.description
        if transfer.delay > 0 { task.earliestBeginDate = Date().addingTimeInterval(transfer.delay) }
        task.resume()
    }
    func cancelAll() async {
        for id in [environment.group + ".share-upload", environment.group + ".app-upload"] {
            for task in await session(id).allTasks { task.cancel() }
        }
    }
    private func accept(_ data: Data, id: String) throws {
        let receipt = try Wire.decoder.decode(ImportReceipt.self, from: data)
        try environment.store.update(id) { guard $0.jobId == nil else { return }; $0.recipeId = receipt.recipeId; $0.jobId = receipt.jobId; $0.phase = "processing"; $0.message = nil }
        try? FileManager.default.removeItem(at: environment.store.bodyURL(id))
    }
    private func bufferKey(_ session: URLSession, _ task: URLSessionTask) -> String { "\(session.configuration.identifier ?? ""):\(task.taskIdentifier)" }
    func urlSession(_ session: URLSession, dataTask: URLSessionDataTask, didReceive data: Data) {
        let key = bufferKey(session, dataTask)
        var value = buffers[key] ?? Data()
        guard value.count + data.count <= 2_097_152 else { downloadErrors[key] = ClientError("The server response is too large."); dataTask.cancel(); return }
        value.append(data); buffers[key] = value
    }
    func urlSession(_ session: URLSession, downloadTask: URLSessionDownloadTask, didFinishDownloadingTo location: URL) {
        let key = bufferKey(session, downloadTask)
        do {
            let file = try FileHandle(forReadingFrom: location); defer { try? file.close() }
            let data = try file.read(upToCount: 2_097_153) ?? Data()
            guard data.count <= 2_097_152 else { throw ClientError("The server response is too large.") }
            buffers[key] = data
        } catch { downloadErrors[key] = error }
    }
    func urlSession(_ session: URLSession, task: URLSessionTask, didCompleteWithError error: Error?) {
        let key = bufferKey(session, task)
        let data = buffers.removeValue(forKey: key) ?? Data()
        let downloadError = downloadErrors.removeValue(forKey: key)
        let failure = downloadError ?? error
        if let transfer = DeliveryTransfer(description: task.taskDescription) {
            do {
                if let failure { throw failure }
                let connection = try environment.currentConnection()
                let delivery = AutomaticDelivery(store: environment.store, connection: connection)
                if let next = try delivery.consume(transfer, data: APIClient.checked(data, task.response)) {
                    try enqueue(next, session: session)
                } else if transfer.step == .publication {
                    try? FileManager.default.removeItem(at: environment.store.root.appendingPathComponent(transfer.itemId + ".publication"))
                }
            } catch { deliveryFailed(transfer, error: error, response: task.response, session: session) }
            return
        }
        guard let id = task.taskDescription else { return }
        do {
            if let failure { throw failure }
            try accept(APIClient.checked(data, task.response), id: id)
            let delivery = try AutomaticDelivery(store: environment.store, connection: environment.currentConnection())
            if let first = try delivery.initial(id) { try enqueue(first, session: session) }
        } catch {
            _ = try? environment.store.update(id) { value in
                value.phase = value.jobId == nil ? "failed" : "delivery_failed"
                value.message = error.localizedDescription
            }
        }
    }
    private func deliveryFailed(_ transfer: DeliveryTransfer, error: Error, response: URLResponse?, session: URLSession) {
        guard (error as? URLError)?.code != .cancelled else { return }
        let retryable = error is URLError || (error as? APIError)?.error.retryable == true
            || (response as? HTTPURLResponse)?.statusCode == 429
        do {
            let delivery = try AutomaticDelivery(store: environment.store, connection: environment.currentConnection())
            guard try delivery.eligible(transfer.itemId) != nil else { return }
            try environment.store.update(transfer.itemId) {
                guard $0.publication == nil else { return }
                $0.phase = retryable && transfer.failures < 5 ? "waiting" : "delivery_failed"
                $0.message = error.localizedDescription
            }
            if retryable && transfer.failures < 5 {
                var retry = transfer; retry.failures += 1
                let header = (response as? HTTPURLResponse)?.value(forHTTPHeaderField: "Retry-After")
                let requestedDelay = header.flatMap(Double.init) ?? 0
                let serverDelay = requestedDelay.isFinite ? max(0, requestedDelay) : 0
                retry.delay = max(serverDelay, min(60, pow(2, Double(retry.failures))))
                try enqueue(retry, session: session)
            }
        } catch { /* Disconnect or unavailable storage: resume from persisted intent on next launch. */ }
    }
    // Background sessions do not deliver redirect callbacks. The application origin must
    // serve uploads directly; foreground enrollment/API requests use NoRedirects.
    func urlSessionDidFinishEvents(forBackgroundURLSession session: URLSession) {
        lock.lock(); let completion = completions.removeValue(forKey: session.configuration.identifier ?? ""); lock.unlock()
        if let completion { DispatchQueue.main.async(execute: completion) }
    }
}
#endif
