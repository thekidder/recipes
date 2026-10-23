import Foundation
import UniformTypeIdentifiers

enum SharedInputLoader {
    static func load(_ items: [Any]) async throws -> ShareInput {
        let providers = items.compactMap { $0 as? NSExtensionItem }.flatMap { $0.attachments ?? [] }
        return try await loadProviders(providers)
    }
    static func loadProviders(_ providers: [NSItemProvider]) async throws -> ShareInput {
        guard !providers.isEmpty else { throw ClientError("No recipe content was shared.") }
        // Safari can advertise a URL alongside its already downloaded Reader PDF.
        // Always use the PDF representation, never fall back to fetching that URL.
        let pdfs = providers.filter { $0.hasItemConformingToTypeIdentifier(UTType.pdf.identifier) }
        if !pdfs.isEmpty {
            guard pdfs.count == 1, providers.allSatisfy({ provider in
                provider === pdfs[0] || (provider.hasItemConformingToTypeIdentifier(UTType.url.identifier)
                    && !provider.hasItemConformingToTypeIdentifier(UTType.fileURL.identifier)
                    && !provider.hasItemConformingToTypeIdentifier(UTType.image.identifier))
            }) else { throw ClientError("Share one recipe PDF at a time, without other files.") }
            let provider = pdfs[0]
            let data = try await fileData(provider, type: UTType.pdf.identifier, maxBytes: PDFInput.maxBytes)
            return try pdfInput(data, filename: provider.suggestedName ?? "Recipe.pdf")
        }
        if providers.allSatisfy({ $0.hasItemConformingToTypeIdentifier(UTType.image.identifier) || $0.hasItemConformingToTypeIdentifier(UTType.fileURL.identifier) }) {
            guard providers.count <= 6 else { throw ClientError("Share at most six recipe photos.") }
            var images: [ImageInput] = []
            for (index, provider) in providers.enumerated() {
                if !provider.hasItemConformingToTypeIdentifier(UTType.image.identifier) {
                    let input = try await fileInput(provider, index: index + 1)
                    if providers.count == 1 { return input }
                    guard input.kind == "images" else { throw ClientError("Share recipe photos together, without other file types.") }
                    images.append(contentsOf: input.images)
                } else {
                    // Request the native representation and normalize HEIC locally.
                    let type = provider.registeredTypeIdentifiers.first {
                        [UTType.png.identifier, UTType.jpeg.identifier, UTType.heic.identifier, UTType.heif.identifier].contains($0)
                    } ?? UTType.image.identifier
                    let data = try await fileData(provider, type: type, maxBytes: ImagePreparation.maxSourceBytes)
                    images.append(try ImagePreparation.prepare(data, index: index + 1))
                }
                guard images.reduce(0, { $0 + $1.data.count }) <= 30 * 1024 * 1024 else { throw ClientError("Share at most 30 MiB of photos at once.") }
            }
            return ShareInput(kind: "images", images: images)
        }
        guard providers.count == 1, let provider = providers.first else { throw ClientError("Share one PDF, one URL, one text item, or a group of photos. Mixed content is not supported.") }
        if provider.hasItemConformingToTypeIdentifier(UTType.url.identifier) {
            let value = try await item(provider, type: UTType.url.identifier)
            if let url = value as? URL {
                return ShareInput(kind: "url", url: url.absoluteString)
            }
            if let string = value as? String { return ShareInput(kind: "url", url: string) }
            if let data = value as? Data {
                if data.starts(with: Data("%PDF-".utf8)) { return try pdfInput(data) }
                return try textInput(data)
            }
        }
        if provider.hasItemConformingToTypeIdentifier(UTType.plainText.identifier) {
            let value = try await item(provider, type: UTType.plainText.identifier)
            if let text = value as? String { return ShareInput.fromText(text) }
            if let data = value as? Data { return try textInput(data) }
            if let url = value as? URL { return try textInput(boundedData(url, maxBytes: 1_048_576)) }
        }
        throw ClientError("Share a Reader PDF, recipe text, a UTF-8 text file, a website URL, or still JPEG/PNG/HEIC photos.")
    }
    private static func fileInput(_ provider: NSItemProvider, index: Int) async throws -> ShareInput {
        try await withCheckedThrowingContinuation { continuation in
            provider.loadItem(forTypeIdentifier: UTType.fileURL.identifier, options: nil) { value, error in
                do {
                    if let error { throw error }
                    let fileURL: URL?
                    if let url = value as? URL { fileURL = url }
                    else if let data = value as? Data { fileURL = URL(dataRepresentation: data, relativeTo: nil) }
                    else if let string = value as? String { fileURL = URL(string: string) }
                    else { fileURL = nil }
                    guard let url = fileURL, url.isFileURL else { throw ClientError("The source app did not provide a readable file.") }
                    let accessed = url.startAccessingSecurityScopedResource()
                    defer { if accessed { url.stopAccessingSecurityScopedResource() } }
                    let input: ShareInput
                    if url.pathExtension.lowercased() == "txt" {
                        input = try textInput(boundedData(url, maxBytes: 1_048_576))
                    } else if url.pathExtension.lowercased() == "pdf" {
                        input = try pdfInput(boundedData(url, maxBytes: PDFInput.maxBytes), filename: url.lastPathComponent)
                    } else {
                        let data = try boundedData(url, maxBytes: ImagePreparation.maxSourceBytes)
                        input = ShareInput(kind: "images", images: [try ImagePreparation.prepare(data, index: index)])
                    }
                    continuation.resume(returning: input)
                } catch { continuation.resume(throwing: error) }
            }
        }
    }
    private static func pdfInput(_ data: Data, filename: String = "Recipe.pdf") throws -> ShareInput {
        let name = filename.lowercased().hasSuffix(".pdf") ? filename : filename + ".pdf"
        let pdf = PDFInput(filename: String(name.prefix(255)), data: data)
        try pdf.validate()
        return ShareInput(kind: "pdf", pdf: pdf)
    }
    private static func textInput(_ data: Data) throws -> ShareInput {
        guard data.count <= 1_048_576, let text = String(data: data, encoding: .utf8) else { throw ClientError("Use a UTF-8 text file smaller than 1 MiB.") }
        return ShareInput.fromText(text)
    }
    private static func boundedData(_ url: URL, maxBytes: Int) throws -> Data {
        let handle = try FileHandle(forReadingFrom: url); defer { try? handle.close() }
        let data = try handle.read(upToCount: maxBytes + 1) ?? Data()
        guard data.count <= maxBytes else { throw ClientError("The shared file exceeds its size limit.") }
        return data
    }
    private static func fileData(_ provider: NSItemProvider, type: String, maxBytes: Int) async throws -> Data {
        try await withCheckedThrowingContinuation { continuation in
            provider.loadFileRepresentation(forTypeIdentifier: type) { url, error in
                do {
                    if let error { throw error }
                    guard let url else { throw ClientError("The source app did not provide its file.") }
                    // Read within the callback: provider temporary URLs are invalid afterward.
                    continuation.resume(returning: try boundedData(url, maxBytes: maxBytes))
                } catch { continuation.resume(throwing: error) }
            }
        }
    }
    private static func item(_ provider: NSItemProvider, type: String) async throws -> NSSecureCoding {
        try await withCheckedThrowingContinuation { continuation in
            provider.loadItem(forTypeIdentifier: type, options: nil) { value, error in
                if let error { continuation.resume(throwing: error); return }
                // URL providers may vend NSURL, its serialized bytes, or a string.
                let providedURL: URL?
                if let url = value as? URL { providedURL = url }
                else if type == UTType.url.identifier, let data = value as? Data { providedURL = URL(dataRepresentation: data, relativeTo: nil) }
                else if type == UTType.url.identifier, let string = value as? String { providedURL = URL(string: string) }
                else { providedURL = nil }
                if let url = providedURL, url.isFileURL {
                    do {
                        let isPDF = type == UTType.url.identifier && url.pathExtension.lowercased() == "pdf"
                        if type == UTType.url.identifier && !isPDF && url.pathExtension.lowercased() != "txt" {
                            throw ClientError("Share a PDF, UTF-8 .txt file, JPEG/PNG/HEIC photos, or a website URL.")
                        }
                        let accessed = url.startAccessingSecurityScopedResource()
                        defer { if accessed { url.stopAccessingSecurityScopedResource() } }
                        let data = try boundedData(url, maxBytes: isPDF ? PDFInput.maxBytes : 1_048_576)
                        if isPDF { try PDFInput(filename: url.lastPathComponent, data: data).validate() }
                        continuation.resume(returning: data as NSData)
                    } catch { continuation.resume(throwing: error) }
                }
                else if let value { continuation.resume(returning: value) }
                else { continuation.resume(throwing: ClientError("The source app did not provide recipe content.")) }
            }
        }
    }
}
