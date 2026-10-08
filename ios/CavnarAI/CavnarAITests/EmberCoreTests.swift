import SwiftUI
import XCTest
@testable import CavnarAI

/// The Ember Core's shader (EmberCore.metal) draws something — a lit heart
/// inside a transparent square — rather than silently compiling to nothing.
/// Rendered off-screen at one frame; the motion is the TimelineView's.
@MainActor
final class EmberCoreTests: XCTestCase {
    private func render(_ view: some View, side: CGFloat) -> CGImage? {
        let r = ImageRenderer(content: view.frame(width: side, height: side))
        r.scale = 2
        return r.cgImage
    }

    private func rgba(_ img: CGImage, _ x: Int, _ y: Int) -> (r: Int, g: Int, b: Int, a: Int) {
        let w = img.width, h = img.height
        var px = [UInt8](repeating: 0, count: w * h * 4)
        let ctx = CGContext(data: &px, width: w, height: h, bitsPerComponent: 8, bytesPerRow: w * 4,
                            space: CGColorSpaceCreateDeviceRGB(), bitmapInfo: CGImageAlphaInfo.premultipliedLast.rawValue)!
        ctx.draw(img, in: CGRect(x: 0, y: 0, width: w, height: h))
        let i = ((h - 1 - y) * w + x) * 4
        return (Int(px[i]), Int(px[i + 1]), Int(px[i + 2]), Int(px[i + 3]))
    }

    func testTheCoreDrawsALitHeartInATransparentSquare() throws {
        // the view's square is size × scale; draw it whole
        let core = EmberCoreView(size: 80, scale: 2.6, interactive: false)
        let img = try XCTUnwrap(render(core.frame(width: 208, height: 208), side: 208))
        let c = rgba(img, img.width / 2, img.height / 2)
        XCTAssertGreaterThan(c.a, 200, "the shell is opaque at the centre")
        XCTAssertGreaterThan(c.r, 120, "the heart glows")
        XCTAssertGreaterThan(c.r, c.b, "ember, not grey")
        let corner = rgba(img, 2, 2)
        XCTAssertLessThan(corner.a, 8, "the glow fades out before the square's edge")
    }

    func testPremultipliedNeverBrighterThanItsAlpha() throws {
        let img = try XCTUnwrap(render(EmberCoreView(size: 60, scale: 2.4, energy: 0.3, interactive: false)
            .frame(width: 144, height: 144), side: 144))
        for (x, y) in [(10, 72), (40, 40), (72, 20), (130, 130)] {
            let p = rgba(img, x * img.width / 144, y * img.height / 144)
            XCTAssertLessThanOrEqual(max(p.r, p.g, p.b), p.a + 2, "colour within alpha at \(x),\(y)")
        }
    }
}
