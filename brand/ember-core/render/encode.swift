// encode.swift framesDir out.mp4 width height loopFrames fadeFrames
// Composites each transparent PNG frame onto the brand's dark ground, cross-
// fades the tail into the head so the clip loops seamlessly, and writes H.264.
import AVFoundation
import CoreGraphics
import Foundation
import ImageIO

let a = CommandLine.arguments
let dir = a[1], out = a[2], W = Int(a[3])!, H = Int(a[4])!, N = Int(a[5])!, FADE = Int(a[6])!
let files = try FileManager.default.contentsOfDirectory(atPath: dir).filter { $0.hasSuffix(".png") }.sorted()
precondition(files.count >= N + FADE, "need \(N + FADE) frames, have \(files.count)")

func load(_ i: Int) -> CGImage {
    let url = URL(fileURLWithPath: dir).appendingPathComponent(files[i]) as CFURL
    let src = CGImageSourceCreateWithURL(url, nil)!
    return CGImageSourceCreateImageAtIndex(src, 0, nil)!
}
let space = CGColorSpace(name: CGColorSpace.sRGB)!
func ground(_ ctx: CGContext) {
    ctx.setFillColor(CGColor(srgbRed: 11 / 255.0, green: 9 / 255.0, blue: 7 / 255.0, alpha: 1))
    ctx.fill(CGRect(x: 0, y: 0, width: W, height: H))
}
// the core square fills the height, centred
func place(_ img: CGImage, _ ctx: CGContext, alpha: CGFloat) {
    let side = CGFloat(H)
    ctx.setAlpha(alpha)
    ctx.interpolationQuality = .high
    ctx.draw(img, in: CGRect(x: (CGFloat(W) - side) / 2, y: 0, width: side, height: side))
    ctx.setAlpha(1)
}
func composite(_ img: CGImage) -> CGImage {
    let ctx = CGContext(data: nil, width: W, height: H, bitsPerComponent: 8, bytesPerRow: 0, space: space,
                        bitmapInfo: CGImageAlphaInfo.premultipliedFirst.rawValue | CGBitmapInfo.byteOrder32Little.rawValue)!
    ground(ctx); place(img, ctx, alpha: 1)
    return ctx.makeImage()!
}

let url = URL(fileURLWithPath: out)
try? FileManager.default.removeItem(at: url)
let writer = try AVAssetWriter(outputURL: url, fileType: .mp4)
let input = AVAssetWriterInput(mediaType: .video, outputSettings: [
    AVVideoCodecKey: AVVideoCodecType.h264, AVVideoWidthKey: W, AVVideoHeightKey: H,
    AVVideoCompressionPropertiesKey: [AVVideoAverageBitRateKey: 10_000_000, AVVideoProfileLevelKey: AVVideoProfileLevelH264HighAutoLevel,
                                      AVVideoMaxKeyFrameIntervalKey: 30]
])
input.expectsMediaDataInRealTime = false
let adaptor = AVAssetWriterInputPixelBufferAdaptor(assetWriterInput: input, sourcePixelBufferAttributes: [
    kCVPixelBufferPixelFormatTypeKey as String: kCVPixelFormatType_32BGRA, kCVPixelBufferWidthKey as String: W, kCVPixelBufferHeightKey as String: H])
writer.add(input)
writer.startWriting(); writer.startSession(atSourceTime: .zero)

for i in 0..<N {
    var pb: CVPixelBuffer?
    CVPixelBufferPoolCreatePixelBuffer(nil, adaptor.pixelBufferPool!, &pb)
    let buf = pb!
    CVPixelBufferLockBaseAddress(buf, [])
    let ctx = CGContext(data: CVPixelBufferGetBaseAddress(buf), width: W, height: H, bitsPerComponent: 8,
                        bytesPerRow: CVPixelBufferGetBytesPerRow(buf), space: space,
                        bitmapInfo: CGImageAlphaInfo.premultipliedFirst.rawValue | CGBitmapInfo.byteOrder32Little.rawValue)!
    let head = composite(load(i))
    if i < FADE {
        // the clip's tail continues into its head: draw the tail frame, then
        // the head over it, weighted by how far into the fade we are
        ctx.draw(composite(load(N + i)), in: CGRect(x: 0, y: 0, width: W, height: H))
        ctx.setAlpha(CGFloat(i) / CGFloat(FADE))
        ctx.draw(head, in: CGRect(x: 0, y: 0, width: W, height: H))
        ctx.setAlpha(1)
    } else {
        ctx.draw(head, in: CGRect(x: 0, y: 0, width: W, height: H))
    }
    // a whisper of grain (±1 of 255) so the dark glow doesn't band in 8 bits
    let base = CVPixelBufferGetBaseAddress(buf)!.assumingMemoryBound(to: UInt8.self)
    let bpr = CVPixelBufferGetBytesPerRow(buf)
    var x: UInt32 = 2463534242 &+ UInt32(i) &* 2654435761
    for row in 0..<H {
        let p = base + row * bpr
        for col in 0..<W {
            x ^= x << 13; x ^= x >> 17; x ^= x << 5
            let n = Int(x % 3) - 1
            for c in 0..<3 { let o = col * 4 + c; p[o] = UInt8(max(0, min(255, Int(p[o]) + n))) }
        }
    }
    CVPixelBufferUnlockBaseAddress(buf, [])
    while !input.isReadyForMoreMediaData { usleep(2000) }
    adaptor.append(buf, withPresentationTime: CMTime(value: CMTimeValue(i), timescale: 30))
}
input.markAsFinished()
let sem = DispatchSemaphore(value: 0)
writer.finishWriting { sem.signal() }
sem.wait()
print(writer.status == .completed ? "wrote \(out)" : "failed: \(String(describing: writer.error))")
