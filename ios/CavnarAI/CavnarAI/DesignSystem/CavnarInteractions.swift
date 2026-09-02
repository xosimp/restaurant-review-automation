import SwiftUI

// The small, reusable interaction pieces the motion audit asked for — the
// house answer to a spinner, the confirmation overlay every successful
// write gets, press feedback for the module tiles, staggered row
// entrances for lists, and the two-factor code cells. Same rules as
// CavnarMotion: one point of color and it's the ember, ease-in-out only,
// and nothing plays unless the app is genuinely working or something
// genuinely changed.

// MARK: - Working (the spinner replacement)

/// The house "still working" state for a block of content that hasn't
/// arrived yet — the ember line sweeping, centered, instead of the system
/// spinner. For a button label use CavnarShimmerText directly.
struct CavnarWorkingLine: View {
    var width: CGFloat = 120
    var color: Color = .cavnarEmber

    var body: some View {
        CavnarShimmerLine(color: color)
            .frame(width: width)
            .frame(maxWidth: .infinity)
    }
}

// MARK: - Posted overlay

/// The confirmation moment for a successful write, as an overlay on the
/// screen that made it: the page dims to paper, the ember travels the wire
/// and lands as a checkmark (CavnarPostedCheck), then `onFinished` fires
/// for the caller to dismiss. Pass a nil label to show nothing. Only ever
/// set the label on the real success response — never optimistically.
private struct CavnarPostedOverlay: ViewModifier {
    var label: String?
    var tone: CavnarPostedTone = .success
    var onFinished: () -> Void

    func body(content: Content) -> some View {
        content
            .overlay {
                if let label {
                    ZStack {
                        Color.cavnarPaper.opacity(0.84).ignoresSafeArea()
                        CavnarPostedCheck(label: label, tone: tone, onFinished: onFinished)
                            .padding(.horizontal, 26)
                            .padding(.vertical, 24)
                            .background(Color.cavnarPaper2)
                            .overlay(RoundedRectangle(cornerRadius: CavnarRadius.card).strokeBorder(Color.cavnarPaper3, lineWidth: 1))
                            .clipShape(RoundedRectangle(cornerRadius: CavnarRadius.card))
                            .shadow(color: .black.opacity(0.45), radius: 24, y: 12)
                    }
                    .transition(.opacity)
                }
            }
            .animation(.easeOut(duration: 0.25), value: label != nil)
    }
}

extension View {
    // tone defaults to .success so every existing call site (Restaurant
    // saved, password changed, email updated, 2FA enabled/disabled, alert
    // settings saved) keeps its current ember/checkmark look unchanged —
    // only a call site that explicitly wants the red "turned off" mirror
    // passes tone: .removed.
    func cavnarPostedOverlay(_ label: String?, tone: CavnarPostedTone = .success, onFinished: @escaping () -> Void) -> some View {
        modifier(CavnarPostedOverlay(label: label, tone: tone, onFinished: onFinished))
    }
}

/// The inline version, for a write that confirms in place rather than
/// closing a sheet: plays the posted check once, holds it, then fades it
/// away on its own so the form underneath is usable again.
struct CavnarInlinePosted: View {
    var label: String
    var onFaded: (() -> Void)? = nil

    @State private var shown = true

    var body: some View {
        CavnarPostedCheck(label: label)
            .frame(maxWidth: .infinity)
            .opacity(shown ? 1 : 0)
            .task {
                try? await Task.sleep(for: .seconds(3.2))
                withAnimation(.easeOut(duration: 0.45)) { shown = false }
                try? await Task.sleep(for: .seconds(0.45))
                onFaded?()
            }
    }
}

// MARK: - Tile press

/// Press feedback for the module tiles: the tile settles down and its
/// ember edge lights, then eases back as navigation happens. Driven
/// explicitly by the tap action (see HomeModuleGrid) rather than the
/// system's `isPressed` — a Button this deep inside a ScrollView/
/// LazyVGrid delays or drops `isPressed` on a fast tap (SwiftUI needs a
/// beat to decide the touch isn't a scroll), so a quick tap showed
/// nothing and only a deliberate hold ever rendered it. Setting `active`
/// from the tap handler is reliable at any tap speed since it isn't
/// gated on that same disambiguation. The on-transition is unanimated —
/// snaps to fully lit in one frame — so it's never caught mid-fade by a
/// tap that releases before an eased engage would have finished; only the
/// release eases out.
struct CavnarTileFlash: ViewModifier {
    var active: Bool

    func body(content: Content) -> some View {
        content
            .overlay(
                RoundedRectangle(cornerRadius: CavnarRadius.card, style: .continuous)
                    .strokeBorder(Color.cavnarEmber2.opacity(active ? 0.95 : 0), lineWidth: 1.5)
            )
            .brightness(active ? 0.05 : 0)
            .shadow(color: Color.cavnarEmber.opacity(active ? 0.45 : 0), radius: 20, y: 4)
            .scaleEffect(active ? 0.965 : 1)
            .animation(active ? nil : .easeOut(duration: 0.3), value: active)
    }
}

extension View {
    func cavnarTileFlash(_ active: Bool) -> some View {
        modifier(CavnarTileFlash(active: active))
    }
}

// MARK: - Row entrances

/// Shared by every row in one list so the stagger only applies to the
/// batch that lands together on first load. The first row to appear
/// starts the clock; rows appearing within a beat of it are staggered by
/// index, rows that scroll into view later (or a later refresh) just fade
/// in with no delay. A class, not @State, so rows share one instance.
@MainActor
final class CavnarEntranceClock {
    private var start: Date?

    /// The delay a row at `index` should wait before rising in.
    func delay(for index: Int) -> Double {
        let now = Date()
        if let start, now.timeIntervalSince(start) < 0.35 {
            return min(Double(index), 9) * 0.055
        }
        start = now
        return index == 0 ? 0 : min(Double(index), 9) * 0.055
    }
}

private struct CavnarRowEntrance: ViewModifier {
    var index: Int
    var clock: CavnarEntranceClock

    @State private var shown = false

    func body(content: Content) -> some View {
        content
            .opacity(shown ? 1 : 0)
            .offset(y: shown ? 0 : 14)
            .onAppear {
                guard !shown else { return }
                withAnimation(.easeOut(duration: 0.42).delay(clock.delay(for: index))) {
                    shown = true
                }
            }
    }
}

extension View {
    /// Fade-and-rise entrance for a list row or grid tile, staggered with
    /// its siblings on first load (see CavnarEntranceClock).
    func cavnarRowEntrance(index: Int, clock: CavnarEntranceClock) -> some View {
        modifier(CavnarRowEntrance(index: index, clock: clock))
    }
}

// MARK: - Code entry

/// Six digit cells for a one-time code, instead of a bare text field. The
/// real input is an invisible TextField underneath so the number pad,
/// autofill from Messages, and paste all still work; the cells are purely
/// what's drawn. The active cell carries a blinking ember caret, each
/// digit pops into its cell as it's typed, the whole row warms while it's
/// being verified, and a wrong code shakes it and flushes the cells red.
struct CavnarCodeEntry: View {
    @Binding var code: String
    var length: Int = 6
    var isVerifying: Bool = false
    var isError: Bool = false
    var focus: FocusState<Bool>.Binding

    @State private var shake: CGFloat = 0

    private var digits: [Character] { Array(code) }

    var body: some View {
        ZStack {
            HStack(spacing: 10) {
                ForEach(0..<length, id: \.self) { i in
                    cell(i)
                }
            }
            .offset(x: shake)

            TextField("", text: $code)
                .keyboardType(.numberPad)
                .textContentType(.oneTimeCode)
                .focused(focus)
                .foregroundStyle(.clear)
                .tint(.clear)
                .opacity(0.02)
                .frame(maxWidth: .infinity)
                .frame(height: 56)
                .contentShape(Rectangle())
        }
        .onChange(of: code) { _, new in
            let cleaned = String(new.filter(\.isNumber).prefix(length))
            if cleaned != new { code = cleaned }
        }
        .onChange(of: isError) { _, failed in
            guard failed else { return }
            Task { @MainActor in
                for dx: CGFloat in [-8, 7, -5, 3, 0] {
                    withAnimation(.easeInOut(duration: 0.06)) { shake = dx }
                    try? await Task.sleep(for: .seconds(0.06))
                }
            }
        }
    }

    private func cell(_ i: Int) -> some View {
        let filled = i < digits.count
        let active = i == digits.count && focus.wrappedValue && !isVerifying
        let border: Color = isError ? .cavnarRed : (active || isVerifying ? .cavnarEmber : .cavnarPaper3)
        return ZStack {
            RoundedRectangle(cornerRadius: CavnarRadius.control, style: .continuous)
                .fill(Color.cavnarPaper2)
            RoundedRectangle(cornerRadius: CavnarRadius.control, style: .continuous)
                .strokeBorder(border, lineWidth: active || isVerifying || isError ? 1.5 : 1)
            if filled {
                Text(String(digits[i]))
                    .font(.cavnarNumber(24, weight: 600))
                    .foregroundStyle(isError ? Color.cavnarRed : Color.cavnarInk)
                    .transition(.scale(scale: 0.5).combined(with: .opacity))
            } else if active {
                CavnarCaret()
            }
        }
        .frame(height: 56)
        .shadow(color: Color.cavnarEmber.opacity(active || isVerifying ? 0.35 : 0), radius: 10)
        .animation(.easeOut(duration: 0.18), value: filled)
        .animation(.easeOut(duration: 0.2), value: active)
        .animation(.easeInOut(duration: 0.3), value: isVerifying)
        .animation(.easeOut(duration: 0.2), value: isError)
    }
}

/// A blinking ember caret, wall-clock driven so it can't be frozen by an
/// ambient transaction.
private struct CavnarCaret: View {
    var body: some View {
        TimelineView(.periodic(from: .now, by: 0.55)) { timeline in
            let on = Int(timeline.date.timeIntervalSinceReferenceDate / 0.55) % 2 == 0
            RoundedRectangle(cornerRadius: 1)
                .fill(Color.cavnarEmber)
                .frame(width: 2, height: 24)
                .opacity(on ? 1 : 0)
        }
    }
}
