import AppKit
import Foundation
import PDFKit
import Vision

let data = FileHandle.standardInput.readDataToEndOfFile()
let useOCR = CommandLine.arguments.contains("--ocr")
guard let document = PDFDocument(data: data), !document.isLocked, document.pageCount <= (useOCR ? 20 : 100) else {
    exit(2)
}

func recognize(_ page: PDFPage) throws -> String {
    let bounds = page.bounds(for: .cropBox)
    guard bounds.width > 0, bounds.height > 0 else { return "" }
    let scale = 2400 / max(bounds.width, bounds.height)
    let size = NSSize(width: bounds.width * scale, height: bounds.height * scale)
    let thumbnail = page.thumbnail(of: size, for: .cropBox)
    guard let image = thumbnail.cgImage(forProposedRect: nil, context: nil, hints: nil) else { return "" }
    let request = VNRecognizeTextRequest()
    request.recognitionLevel = .accurate
    request.usesLanguageCorrection = false
    if #available(macOS 13.0, *) {
        request.automaticallyDetectsLanguage = true
    }
    try VNImageRequestHandler(cgImage: image).perform([request])

    // Vision supplies observations in normalized image coordinates. Rejoin words
    // on the same statement row before sending the text to the conservative parser.
    let words = (request.results ?? []).compactMap { observation -> (String, CGRect)? in
        guard let match = observation.topCandidates(1).first, match.confidence >= 0.5 else { return nil }
        return (match.string, observation.boundingBox)
    }.sorted { $0.1.midY > $1.1.midY }
    var rows: [[(String, CGRect)]] = []
    for word in words {
        if let last = rows.last?.first, abs(last.1.midY - word.1.midY) < min(last.1.height, word.1.height) * 0.5 {
            rows[rows.count - 1].append(word)
        } else {
            rows.append([word])
        }
    }
    return rows.map { row in row.sorted { $0.1.minX < $1.1.minX }.map { $0.0 }.joined(separator: "  ") }
        .joined(separator: "\n")
}

for index in 0..<document.pageCount {
    if let page = document.page(at: index) {
        let contents = page.string ?? ""
        if !contents.trimmingCharacters(in: .whitespacesAndNewlines).isEmpty {
            print(contents)
        } else if useOCR {
            do {
                print(try recognize(page))
            } catch {
                exit(3)
            }
        }
    }
    print("\u{000C}")
}
