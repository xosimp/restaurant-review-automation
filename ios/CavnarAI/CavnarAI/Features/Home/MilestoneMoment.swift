import SwiftUI

/// The first celebration this app has ever had.
///
/// The delight audit found exactly one in the whole product — a web modal
/// that fires when every review has a reply — gated on
/// `localStorage['cavnar_congrats_shown']`, so its own promise ("This won't
/// appear again") was one the storage could not keep, and iOS had nothing
/// at all. A milestone is now a row on the server (UNIQUE on restaurant_id
/// and key), so "once" means once across every device the owner opens.
///
/// DESIGN STANCE. `DESIGN_SYSTEM.md` §11: ember is the only accent that
/// moves, no bounce, no spring overshoot, and motion tells you something
/// changed rather than decorating a static screen. So this is not confetti.
/// It is the seal drawing itself in, one ember pulse, and a sentence that
/// says what was actually counted — the restraint IS the celebration, in a
/// product whose whole credibility rests on never overclaiming.
///
/// It is also always dismissible and never blocks. An owner opening the app
/// during service to answer a one-star review must not have to dismiss a
/// party first.
struct MilestoneMoment: View {
    let milestone: HomeFollowThroughViewModel.Milestones.Item
    var onDismiss: () -> Void

    @Environment(\.accessibilityReduceMotion) private var reduceMotion
    @State private var appeared = false

    /// One glyph per kind. Nothing here is a trophy or a star: this product
    /// does not award points, and a medal would say it does.
    private var glyph: String {
        switch milestone.kind {
        case "savings":      return "chart.line.uptrend.xyaxis"
        case "anniversary":  return "calendar"
        case "response_rate": return "checkmark.bubble"
        case "goal":         return "target"
        default:             return "sparkles"
        }
    }

    private var kicker: String {
        switch milestone.kind {
        case "savings":      return "MEASURED RESULTS"
        case "anniversary":  return "A MILESTONE"
        case "response_rate": return "EVERY REVIEW ANSWERED"
        case "goal":         return "GOAL MET"
        default:             return "A MILESTONE"
        }
    }

    var body: some View {
        ZStack {
            Color.cavnarInk.opacity(0.55)
                .ignoresSafeArea()
                .onTapGesture { onDismiss() }

            VStack(spacing: 18) {
                GlowBadge(systemImage: glyph, size: 64)
                    .scaleEffect(appeared || reduceMotion ? 1 : 0.9)
                    .opacity(appeared || reduceMotion ? 1 : 0)

                VStack(spacing: 8) {
                    Text(kicker)
                        .font(.cavnarBody(11, weight: 700))
                        .tracking(1.6)
                        .foregroundStyle(Color.cavnarEmber2)
                    Text(milestone.title)
                        .font(.cavnarHeadline(22))
                        .foregroundStyle(Color.cavnarInk)
                        .multilineTextAlignment(.center)
                    if let body = milestone.body {
                        Text(body)
                            .font(.cavnarBody(14.5))
                            .foregroundStyle(Color.cavnarInk3)
                            .multilineTextAlignment(.center)
                            .lineSpacing(3)
                            .fixedSize(horizontal: false, vertical: true)
                    }
                }

                Button {
                    Haptic.light()
                    onDismiss()
                } label: {
                    Text("Got it")
                        .font(.cavnarBody(15, weight: 700))
                        .foregroundStyle(Color.cavnarPaper)
                        .frame(maxWidth: .infinity)
                        .padding(.vertical, 13)
                        .background(Color.cavnarEmber, in: RoundedRectangle(cornerRadius: 10,
                                                                            style: .continuous))
                }
                .buttonStyle(.plain)
            }
            .padding(26)
            .frame(maxWidth: 360)
            .background(Color.cavnarSurface, in: RoundedRectangle(cornerRadius: 16,
                                                                  style: .continuous))
            .overlay(
                RoundedRectangle(cornerRadius: 16, style: .continuous)
                    .stroke(Color.cavnarPaper3.opacity(0.6), lineWidth: 1)
            )
            .padding(.horizontal, 24)
            .opacity(appeared || reduceMotion ? 1 : 0)
            .offset(y: appeared || reduceMotion ? 0 : 8)
        }
        .onAppear {
            // One success haptic, once. The moment is rare by construction,
            // so this never becomes the buzz people learn to ignore.
            Task { await Haptic.success() }
            guard !reduceMotion else { return }
            withAnimation(.easeOut(duration: 0.42)) { appeared = true }
        }
        .accessibilityElement(children: .contain)
        .accessibilityLabel("\(milestone.title). \(milestone.body ?? "")")
    }
}
