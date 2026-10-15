// Deterministic vector drawing for the app icon; no external assets or packages.
// Run: swift client_app/Scripts/generate_app_icon.swift
import AppKit
import ImageIO
import UniformTypeIdentifiers

let scriptURL = URL(fileURLWithPath: #filePath)
let output = scriptURL.deletingLastPathComponent().deletingLastPathComponent()
    .appendingPathComponent("App/Assets.xcassets/AppIcon.appiconset/AppIcon.png")
let colorSpace = CGColorSpace(name: CGColorSpace.sRGB)!
let canvas = CGContext(data: nil, width: 1024, height: 1024, bitsPerComponent: 8,
                       bytesPerRow: 0, space: colorSpace,
                       bitmapInfo: CGImageAlphaInfo.noneSkipLast.rawValue)!
func color(_ red: CGFloat, _ green: CGFloat, _ blue: CGFloat) -> CGColor {
    CGColor(colorSpace: colorSpace, components: [red / 255, green / 255, blue / 255, 1])!
}
func roundedRect(_ rect: CGRect, radius: CGFloat, fill: CGColor) {
    canvas.setFillColor(fill)
    canvas.addPath(CGPath(roundedRect: rect, cornerWidth: radius, cornerHeight: radius, transform: nil))
    canvas.fillPath()
}
let green = color(30, 75, 58)
let cream = color(255, 247, 226)
canvas.setFillColor(green)
canvas.fill(CGRect(x: 0, y: 0, width: 1024, height: 1024))
// Offset card, then the main recipe card.
roundedRect(CGRect(x: 246, y: 178, width: 566, height: 650), radius: 58, fill: color(83, 122, 91))
roundedRect(CGRect(x: 200, y: 218, width: 566, height: 650), radius: 58, fill: cream)
// Simple sprig on the upper half of the card.
canvas.setStrokeColor(green)
canvas.setLineWidth(22)
canvas.setLineCap(.round)
canvas.move(to: CGPoint(x: 474, y: 553))
canvas.addCurve(to: CGPoint(x: 501, y: 755), control1: CGPoint(x: 465, y: 626), control2: CGPoint(x: 500, y: 682))
canvas.strokePath()
let leftLeaf = CGMutablePath()
leftLeaf.move(to: CGPoint(x: 483, y: 628))
leftLeaf.addCurve(to: CGPoint(x: 374, y: 738), control1: CGPoint(x: 391, y: 614), control2: CGPoint(x: 368, y: 677))
leftLeaf.addCurve(to: CGPoint(x: 483, y: 628), control1: CGPoint(x: 463, y: 748), control2: CGPoint(x: 499, y: 698))
canvas.setFillColor(green)
canvas.addPath(leftLeaf)
canvas.fillPath()
let rightLeaf = CGMutablePath()
rightLeaf.move(to: CGPoint(x: 488, y: 690))
rightLeaf.addCurve(to: CGPoint(x: 609, y: 787), control1: CGPoint(x: 483, y: 773), control2: CGPoint(x: 544, y: 804))
rightLeaf.addCurve(to: CGPoint(x: 488, y: 690), control1: CGPoint(x: 619, y: 709), control2: CGPoint(x: 558, y: 676))
canvas.addPath(rightLeaf)
canvas.fillPath()
// Ingredient bullets and text lines, readable at small sizes.
for (index, width) in [CGFloat(276), 218, 251].enumerated() {
    let y = CGFloat(470 - index * 66)
    roundedRect(CGRect(x: 290, y: y, width: 26, height: 26), radius: 13, fill: color(192, 105, 64))
    roundedRect(CGRect(x: 348, y: y + 1, width: width, height: 24), radius: 12, fill: green)
}
let image = canvas.makeImage()!
let destination = CGImageDestinationCreateWithURL(output as CFURL, UTType.png.identifier as CFString, 1, nil)!
CGImageDestinationAddImage(destination, image, nil)
guard CGImageDestinationFinalize(destination) else { fatalError("Could not write app icon") }
print("Generated \(output.path)")
