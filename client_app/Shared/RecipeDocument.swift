import Foundation

/// Presentation of the server's small erecipe/v1 dialect. Inline emphasis and
/// escapes are rendered by Foundation; block structure stays native and reflows.
struct RecipeDocument {
    struct Block: Identifiable {
        enum Kind: Equatable { case heading, group, ingredient, step(Int), note }
        let id: Int
        let kind: Kind
        var text: String
    }
    let blocks: [Block]
    let metadata: [String: String]

    init(markdown: String) {
        let lines = markdown.replacingOccurrences(of: "\r\n", with: "\n").components(separatedBy: "\n")
        var start = 0
        var metadata: [String: String] = [:]
        if lines.first == "---", let end = lines.dropFirst().firstIndex(of: "---") {
            // PyYAML may wrap long single-line values over indented lines.
            var fields: [(String, String)] = []
            for line in lines[1..<end] {
                if line.first?.isWhitespace == true, !fields.isEmpty {
                    fields[fields.count - 1].1 += " " + line.trimmingCharacters(in: .whitespaces)
                } else if let colon = line.firstIndex(of: ":") {
                    fields.append((String(line[..<colon]), String(line[line.index(after: colon)...]).trimmingCharacters(in: .whitespaces)))
                }
            }
            for (key, raw) in fields {
                var value = raw
                if value.hasPrefix("\""), let data = value.data(using: .utf8),
                   let decoded = try? JSONDecoder().decode(String.self, from: data) { value = decoded }
                else if value.hasPrefix("'"), value.hasSuffix("'") { value = String(value.dropFirst().dropLast()).replacingOccurrences(of: "''", with: "'") }
                else if value.hasPrefix(">-") || value.hasPrefix("|-") { value = String(value.dropFirst(2)).trimmingCharacters(in: .whitespaces) }
                if !value.isEmpty && value != "null" && value != "~" { metadata[key] = value }
            }
            start = end + 1
        }
        // CommonMark also allows Setext section headings and closing ATX hashes.
        var bodyLines: [String] = []
        for raw in lines.dropFirst(start) {
            let line = raw.trimmingCharacters(in: .whitespaces)
            if line.range(of: #"^-{3,}$"#, options: .regularExpression) != nil,
               let previous = bodyLines.last, ["Ingredients", "Method", "Notes"].contains(previous.trimmingCharacters(in: .whitespaces)) {
                bodyLines[bodyLines.count - 1] = "## " + previous.trimmingCharacters(in: .whitespaces)
            } else { bodyLines.append(raw) }
        }
        var blocks: [Block] = []
        var section = ""
        var number = 0
        var paragraphBreak = false
        for raw in bodyLines {
            var line = raw.trimmingCharacters(in: .whitespaces)
            if line.hasPrefix("##") {
                line = line.replacingOccurrences(of: #"\s+#+\s*$"#, with: "", options: .regularExpression)
            }
            if line.isEmpty { paragraphBreak = true; continue }
            var kind: Block.Kind?
            var text = line
            if line.hasPrefix("## ") {
                section = String(line.dropFirst(3)); text = section; kind = .heading
                if section == "Method" { number = 0 }
            } else if line.hasPrefix("### ") {
                text = String(line.dropFirst(4)); kind = .group
            } else if let range = line.range(of: #"^[-*+]\s+"#, options: .regularExpression) {
                text = String(line[range.upperBound...]); kind = section == "Ingredients" ? .ingredient : .note
            } else if let range = line.range(of: #"^\d+[.)]\s+"#, options: .regularExpression) {
                number += 1; text = String(line[range.upperBound...]); kind = .step(number)
            }
            if let kind { blocks.append(Block(id: blocks.count, kind: kind, text: text)) }
            else if let last = blocks.last, last.kind != .heading, last.kind != .group, !(paragraphBreak && section == "Notes") {
                blocks[blocks.count - 1].text += " " + text
            } else { blocks.append(Block(id: blocks.count, kind: .note, text: text)) }
            paragraphBreak = false
        }
        self.blocks = blocks
        self.metadata = metadata
    }
}
