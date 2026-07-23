// qr_ref.swift — two independent opinions about a QR code, neither of them ours.
//
// Called by tests/qr_ref.py. Two subcommands:
//
//   generate <payload> <L|M|Q|H>   Apple's CIQRCodeGenerator encodes the same
//                                  payload at the same EC level. Prints the
//                                  module matrix as rows of 0/1, quiet zone
//                                  stripped. This is a REFERENCE ENCODER —
//                                  a third-party implementation of ISO 18004 —
//                                  so a matrix that differs from ours means one
//                                  of the two is wrong, and it is not Apple's.
//
//   decode <file.png>              Vision's VNDetectBarcodesRequest reads the
//                                  image we rendered and prints what it found.
//                                  This is the check that matters most: it is
//                                  a real decoder, of the kind a phone runs,
//                                  and it has never seen orchestra/qr.py.
//
// Exit code is non-zero on any failure, and every failure prints to stderr.

import Foundation
import CoreImage
import Vision
import AppKit

func fail(_ message: String) -> Never {
    FileHandle.standardError.write((message + "\n").data(using: .utf8)!)
    exit(1)
}

// Render a CIImage at one pixel per module and read the modules back out.
func matrix(from image: CIImage) -> [[Int]] {
    let context = CIContext(options: [.useSoftwareRenderer: true])
    let extent = image.extent
    guard let cg = context.createCGImage(image, from: extent) else {
        fail("could not rasterise the generated code")
    }
    let w = cg.width, h = cg.height
    var pixels = [UInt8](repeating: 0, count: w * h * 4)
    guard let ctx = CGContext(data: &pixels, width: w, height: h,
                              bitsPerComponent: 8, bytesPerRow: w * 4,
                              space: CGColorSpaceCreateDeviceRGB(),
                              bitmapInfo: CGImageAlphaInfo.premultipliedLast.rawValue)
    else { fail("no bitmap context") }
    ctx.draw(cg, in: CGRect(x: 0, y: 0, width: w, height: h))

    // The CGContext coordinate system is bottom-left, but the BUFFER it writes
    // is stored top row first — so row 0 of `pixels` is already the top row of
    // the image and must not be reversed. Reversing it produced a matrix that
    // was a perfect QR code upside down, whose finder patterns sat at
    // top-left/bottom-left/bottom-right instead of top-left/top-right/
    // bottom-left. It compared as "366 of 841 modules differ", which reads
    // exactly like an encoder bug and was not one.
    var rows: [[Int]] = []
    for y in 0..<h {
        var row: [Int] = []
        for x in 0..<w {
            let a = pixels[(y * w + x) * 4 + 3]
            let r = pixels[(y * w + x) * 4 + 0]
            // The generator emits black-on-transparent. Dark == opaque and not
            // white; treating transparent as light is what strips the margin.
            row.append((a > 127 && r < 128) ? 1 : 0)
        }
        rows.append(row)
    }
    return rows
}

// CIQRCodeGenerator surrounds the code with a quiet zone of its own choosing.
// Trim every fully-light edge row and column so what is left is exactly the
// symbol — which is what orchestra/qr.py's `encode` returns.
func trim(_ m: [[Int]]) -> [[Int]] {
    var rows = m
    while let f = rows.first, f.allSatisfy({ $0 == 0 }) { rows.removeFirst() }
    while let l = rows.last, l.allSatisfy({ $0 == 0 }) { rows.removeLast() }
    guard !rows.isEmpty else { return rows }
    var left = 0
    while rows.allSatisfy({ $0[left] == 0 }) { left += 1 }
    var right = rows[0].count - 1
    while rows.allSatisfy({ $0[right] == 0 }) { right -= 1 }
    return rows.map { Array($0[left...right]) }
}

let args = CommandLine.arguments
guard args.count >= 2 else { fail("usage: qr_ref generate <payload> <level> | decode <png>") }

switch args[1] {

case "generate":
    guard args.count == 4 else { fail("generate needs <payload> <level>") }
    let payload = args[2], level = args[3]
    guard let filter = CIFilter(name: "CIQRCodeGenerator") else {
        fail("CIQRCodeGenerator is unavailable")
    }
    filter.setValue(payload.data(using: .isoLatin1) ?? Data(payload.utf8),
                    forKey: "inputMessage")
    filter.setValue(level, forKey: "inputCorrectionLevel")
    guard let out = filter.outputImage else { fail("the filter produced nothing") }
    for row in trim(matrix(from: out)) {
        print(row.map(String.init).joined())
    }

case "decode":
    guard args.count == 3 else { fail("decode needs <png>") }
    guard let data = FileManager.default.contents(atPath: args[2]),
          let image = NSImage(data: data),
          let cg = image.cgImage(forProposedRect: nil, context: nil, hints: nil)
    else { fail("could not read \(args[2])") }

    let request = VNDetectBarcodesRequest()
    request.symbologies = [.qr]
    let handler = VNImageRequestHandler(cgImage: cg, options: [:])
    do { try handler.perform([request]) } catch { fail("Vision failed: \(error)") }
    guard let results = request.results, !results.isEmpty else {
        fail("VISION FOUND NO BARCODE — the image is not a readable QR code")
    }
    for r in results {
        print(r.payloadStringValue ?? "<undecodable payload>")
    }

default:
    fail("unknown subcommand \(args[1])")
}
