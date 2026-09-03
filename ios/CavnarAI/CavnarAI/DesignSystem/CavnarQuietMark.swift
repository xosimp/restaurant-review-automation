import SwiftUI

/// The in-app "quiet hours are silencing alerts right now" badge — Apple's
/// own do-not-disturb glyph, but deliberately not their purple: this app's
/// rule is one accent color and it's always ember, and ember specifically
/// means "needs attention," the opposite of what this badge is saying. It
/// stays a neutral ink tone instead, same family as every other "off"
/// state in Account (2FA off, alerts off).
///
/// The crescent is drawn as a filled disc with a smaller disc "bitten" out
/// of it in the badge's own background color, rather than a hairline
/// stroke outline — a stroked crescent this thin didn't read clearly at
/// toolbar size (34pt); a filled silhouette matches how the bell/building
/// icons beside it are drawn too.
struct CavnarQuietMark: View {
    var size: CGFloat = 34

    private var badgeFill: Color { Color.white.opacity(0.07) }

    var body: some View {
        ZStack {
            Circle().fill(badgeFill)
            Circle().strokeBorder(Color.white.opacity(0.12), lineWidth: 1)
            crescent
        }
        .frame(width: size, height: size)
    }

    private var crescent: some View {
        let d = size * 0.46
        return ZStack {
            Circle().fill(Color.cavnarInk2).frame(width: d, height: d)
            // Drawn in the exact same fill as the badge's own background —
            // sitting directly on top of the disc above, it reads as a
            // bite taken out of it rather than a separate dot.
            Circle().fill(badgeFill).frame(width: d * 0.8, height: d * 0.8)
                .offset(x: d * 0.3, y: -d * 0.12)
        }
    }
}
