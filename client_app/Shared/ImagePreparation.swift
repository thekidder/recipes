import Foundation
import ImageIO
import UniformTypeIdentifiers

// Shared by the Photos picker and Share extension. Decode only bounded thumbnails
// when converting camera photos so six full-resolution bitmaps never stay in memory.
enum ImagePreparation {
    static let maxSourceBytes = 30 * 1024 * 1024
    static let maxUploadBytes = 10 * 1024 * 1024
    static let maxUploadPixels = 20_000_000

    static func prepare(_ data: Data, index: Int) throws -> ImageInput {
        try autoreleasepool {
            guard !data.isEmpty, data.count <= maxSourceBytes,
                  let source = CGImageSourceCreateWithData(data as CFData, [kCGImageSourceShouldCache: false] as CFDictionary),
                  let type = CGImageSourceGetType(source) as String?,
                  [UTType.jpeg.identifier, UTType.png.identifier, UTType.heic.identifier, UTType.heif.identifier].contains(type),
                  CGImageSourceGetCount(source) == 1,
                  let properties = CGImageSourceCopyPropertiesAtIndex(source, 0, nil) as? [CFString: Any],
                  let width = properties[kCGImagePropertyPixelWidth] as? Int,
                  let height = properties[kCGImagePropertyPixelHeight] as? Int,
                  width > 0, height > 0, width <= 80_000_000 / height else {
                throw ClientError("Use still JPEG, PNG, or HEIC photos up to 30 MiB and 80 megapixels each. Animated images are not supported.")
            }
            let isPNG = type == UTType.png.identifier
            let preserve = (isPNG || type == UTType.jpeg.identifier) && data.count <= maxUploadBytes && width <= maxUploadPixels / height
            let options: [CFString: Any] = [
                kCGImageSourceCreateThumbnailFromImageAlways: true,
                kCGImageSourceCreateThumbnailWithTransform: true,
                kCGImageSourceThumbnailMaxPixelSize: preserve ? 240 : 4096,
                kCGImageSourceShouldCacheImmediately: true
            ]
            guard let image = CGImageSourceCreateThumbnailAtIndex(source, 0, options as CFDictionary),
                  CGImageSourceGetStatusAtIndex(source, 0) == .statusComplete else {
                throw ClientError("A recipe photo could not be decoded. Try exporting it again.")
            }
            var output = data
            if !preserve {
                let converted = NSMutableData()
                let outputType = isPNG ? UTType.png : UTType.jpeg
                guard let destination = CGImageDestinationCreateWithData(converted, outputType.identifier as CFString, 1, nil) else {
                    throw ClientError("The photo could not be prepared for upload.")
                }
                CGImageDestinationAddImage(destination, image, [kCGImageDestinationLossyCompressionQuality: 0.9] as CFDictionary)
                guard CGImageDestinationFinalize(destination) else { throw ClientError("The photo could not be converted.") }
                output = converted as Data
            }
            guard output.count <= maxUploadBytes else { throw ClientError("The prepared photo exceeds 10 MiB. Share a smaller image.") }
            return ImageInput(filename: "recipe-\(index).\(isPNG ? "png" : "jpg")", mediaType: isPNG ? "image/png" : "image/jpeg", data: output)
        }
    }

    static func validate(_ image: ImageInput, pixelLimit: Int) throws {
        guard !image.data.isEmpty,
              let source = CGImageSourceCreateWithData(image.data as CFData, [kCGImageSourceShouldCache: false] as CFDictionary),
              CGImageSourceGetCount(source) == 1,
              let type = CGImageSourceGetType(source) as String?,
              (type == UTType.png.identifier && image.mediaType == "image/png") || (type == UTType.jpeg.identifier && image.mediaType == "image/jpeg"),
              let properties = CGImageSourceCopyPropertiesAtIndex(source, 0, nil) as? [CFString: Any],
              let width = properties[kCGImagePropertyPixelWidth] as? Int,
              let height = properties[kCGImagePropertyPixelHeight] as? Int,
              width > 0, height > 0, width <= pixelLimit / height else {
            throw ClientError("A photo does not meet the server’s format or pixel limits.")
        }
    }

    static func thumbnail(_ data: Data) -> CGImage? {
        guard let source = CGImageSourceCreateWithData(data as CFData, [kCGImageSourceShouldCache: false] as CFDictionary) else { return nil }
        return CGImageSourceCreateThumbnailAtIndex(source, 0, [
            kCGImageSourceCreateThumbnailFromImageAlways: true,
            kCGImageSourceCreateThumbnailWithTransform: true,
            kCGImageSourceThumbnailMaxPixelSize: 240
        ] as CFDictionary)
    }
}
