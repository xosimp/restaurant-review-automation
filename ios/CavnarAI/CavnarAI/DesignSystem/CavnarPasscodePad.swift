import SwiftUI

/// The passcode entry surface — six ember dots over a glass keypad. Used by
/// LockedView (unlock) and AppPasscodeSheet (set / change / remove), so
/// entering a passcode looks and feels identical everywhere it happens.
///
/// Motion rules follow CavnarMotion: the ember is the only color that
/// moves. Each digit lands as a dot scaling in with one thin ripple; a
/// wrong code turns the row red and shakes it once; while verifying the
/// row breathes. The keypad rises in with a short stagger on first
/// appearance. Reduce Motion drops the ripple, shake and stagger and keeps
/// only the color changes.
struct CavnarPasscodePad: View {
    @Binding var code: String
    var length: Int = AppPasscode.length
    var isError: Bool = false
    var isVerifying: Bool = false
    var disabled: Bool = false
    /// Renders a Face ID / Touch ID key in the keypad's bottom-left slot.
    var biometrySymbol: String? = nil
    var onBiometry: (() -> Void)? = nil
    /// Fires once the sixth digit lands.
    var onComplete: (String) -> Void

    @Environment(\.accessibilityReduceMotion) private var reduceMotion
    @State private var shake: CGFloat = 0
    @State private var rippleAt: Int? = nil
    @State private var rippleID = 0
    @State private var revealed = false

    private var digits: Int { code.count }

    var body: some View {
        VStack(spacing: 44) {
            dots
            keypad
        }
        .onChange(of: code) { old, new in
            let cleaned = String(new.filter(\.isNumber).prefix(length))
            if cleaned != new { code = cleaned; return }
            if cleaned.count > old.count, !reduceMotion {
                rippleAt = cleaned.count - 1
                rippleID += 1
            }
            if cleaned.count == length { onComplete(cleaned) }
        }
        .onChange(of: isError) { _, failed in
            guard failed else { return }
            guard !reduceMotion else { return }
            Task { @MainActor in
                for dx: CGFloat in [-10, 9, -7, 5, -3, 0] {
                    withAnimation(.easeInOut(duration: 0.055)) { shake = dx }
                    try? await Task.sleep(for: .seconds(0.055))
                }
            }
        }
        .onAppear {
            if reduceMotion { revealed = true } else {
                withAnimation { revealed = true }
            }
        }
    }

    // MARK: - Dots

    private var dots: some View {
        TimelineView(.animation(minimumInterval: 1.0 / 30.0, paused: !isVerifying)) { timeline in
            let t = timeline.date.timeIntervalSinceReferenceDate
            let breathe = isVerifying ? 0.55 + 0.45 * (0.5 + 0.5 * sin(t * 2 * .pi / 1.1)) : 1
            HStack(spacing: 20) {
                ForEach(0..<length, id: \.self) { i in
                    dot(i)
                }
            }
            .opacity(breathe)
        }
        .offset(x: shake)
        .accessibilityElement(children: .ignore)
        .accessibilityLabel("Passcode, \(digits) of \(length) digits entered")
    }

    private func dot(_ i: Int) -> some View {
        let filled = i < digits
        let fill: [Color] = isError ? [.cavnarRed, .cavnarRed] : [.cavnarEmber2, .cavnarEmber]
        let ring: Color = isError ? .cavnarRed.opacity(0.7) : .cavnarInk3.opacity(0.45)
        return ZStack {
            Circle()
                .strokeBorder(ring, lineWidth: 1.5)
                .frame(width: 15, height: 15)
            if filled {
                Circle()
                    .fill(RadialGradient(colors: fill, center: UnitPoint(x: 0.4, y: 0.36), startRadius: 0, endRadius: 9))
                    .frame(width: 15, height: 15)
                    .shadow(color: (isError ? Color.cavnarRed : Color.cavnarEmber).opacity(0.7), radius: 9)
                    .transition(.scale(scale: 0.3).combined(with: .opacity))
            }
            if rippleAt == i {
                CavnarRippleBurst(color: .cavnarEmber, fromDiameter: 15, toDiameter: 46, rings: 1, duration: 0.55)
                    .id(rippleID)
            }
        }
        .frame(width: 22, height: 22)
        .animation(.easeOut(duration: 0.18), value: filled)
        .animation(.easeOut(duration: 0.2), value: isError)
    }

    // MARK: - Keypad

    private static let keyRows: [[String]] = [["1", "2", "3"], ["4", "5", "6"], ["7", "8", "9"]]

    private var keypad: some View {
        VStack(spacing: 16) {
            ForEach(Array(Self.keyRows.enumerated()), id: \.offset) { rowIndex, row in
                HStack(spacing: 22) {
                    ForEach(Array(row.enumerated()), id: \.offset) { colIndex, digit in
                        digitKey(digit)
                            .keyReveal(revealed, index: rowIndex * 3 + colIndex, reduceMotion: reduceMotion)
                    }
                }
            }
            HStack(spacing: 22) {
                Group {
                    if let biometrySymbol, let onBiometry {
                        key(accessibility: "Unlock with biometrics") {
                            Haptic.light()
                            onBiometry()
                        } label: {
                            Image(systemName: biometrySymbol)
                                .font(.system(size: 26, weight: .medium))
                                .foregroundStyle(Color.cavnarEmber2)
                        }
                    } else {
                        Color.clear.frame(width: Self.keySize, height: Self.keySize)
                    }
                }
                .keyReveal(revealed, index: 9, reduceMotion: reduceMotion)

                digitKey("0")
                    .keyReveal(revealed, index: 10, reduceMotion: reduceMotion)

                key(accessibility: "Delete") {
                    guard !code.isEmpty else { return }
                    Haptic.selection()
                    code.removeLast()
                } label: {
                    Image(systemName: "delete.left")
                        .font(.system(size: 22, weight: .medium))
                        .foregroundStyle(Color.cavnarInk2)
                }
                .simultaneousGesture(
                    LongPressGesture(minimumDuration: 0.5).onEnded { _ in
                        guard !code.isEmpty else { return }
                        Haptic.medium()
                        code = ""
                    }
                )
                .opacity(code.isEmpty ? 0.35 : 1)
                .animation(.easeOut(duration: 0.2), value: code.isEmpty)
                .keyReveal(revealed, index: 11, reduceMotion: reduceMotion)
            }
        }
        .disabled(disabled || isVerifying)
        .opacity(disabled ? 0.45 : 1)
        .animation(.easeOut(duration: 0.25), value: disabled)
    }

    private static let letters: [String: String] = [
        "2": "ABC", "3": "DEF", "4": "GHI", "5": "JKL", "6": "MNO", "7": "PQRS", "8": "TUV", "9": "WXYZ",
    ]

    private func digitKey(_ digit: String) -> some View {
        key(accessibility: digit) {
            guard code.count < length else { return }
            Haptic.selection()
            code.append(digit)
        } label: {
            VStack(spacing: 1) {
                Text(digit)
                    .font(.cavnarNumber(30, weight: 500))
                    .foregroundStyle(Color.cavnarInk)
                // The phone-dial letters — a familiar cue that also gives
                // each key a second visual weight instead of one floating
                // numeral. Reserved (invisible) on 0/1 so every digit sits
                // at the same height.
                Text(Self.letters[digit] ?? "ABC")
                    .font(.cavnarBody(10, weight: 700))
                    .tracking(1.6)
                    .foregroundStyle(Color.cavnarInk3.opacity(0.85))
                    .opacity(Self.letters[digit] == nil ? 0 : 1)
            }
            .padding(.top, 3)
        }
    }

    static let keySize: CGFloat = 74

    // Obsidian — the same two surface tones GlowBadge's tile uses, so the
    // keys and the app's badges read as one material.
    private static let keyTop = Color(red: 0.173, green: 0.173, blue: 0.180)      // #2C2C2E
    private static let keyBottom = Color(red: 0.094, green: 0.094, blue: 0.102)   // #18181A

    private func key<Label: View>(accessibility: String, action: @escaping () -> Void, @ViewBuilder label: () -> Label) -> some View {
        Button(action: action) {
            label()
                .frame(width: Self.keySize, height: Self.keySize)
                .background(
                    Circle().fill(
                        LinearGradient(colors: [Self.keyTop, Self.keyBottom], startPoint: .top, endPoint: .bottom)
                    )
                )
                // Lit edge: ember at the top-left corner, falling to a plain
                // hairline — GlowBadge's own recipe, so a key reads as a
                // solid object catching the same light as every badge.
                .overlay(
                    Circle().strokeBorder(
                        LinearGradient(
                            stops: [
                                .init(color: Color.cavnarEmber2.opacity(0.75), location: 0),
                                .init(color: Color.cavnarEmber.opacity(0.28), location: 0.4),
                                .init(color: Color.white.opacity(0.10), location: 1),
                            ],
                            startPoint: .topLeading, endPoint: .bottomTrailing
                        ),
                        lineWidth: 1.2
                    )
                )
                // A lip of light along the top inside the edge.
                .overlay(
                    Circle()
                        .inset(by: 2)
                        .strokeBorder(
                            LinearGradient(colors: [Color.white.opacity(0.12), Color.white.opacity(0)], startPoint: .top, endPoint: .center),
                            lineWidth: 1
                        )
                )
                .shadow(color: .black.opacity(0.55), radius: 8, x: 0, y: 5)
                .contentShape(Circle())
        }
        .buttonStyle(PasscodeKeyStyle())
        .accessibilityLabel(accessibility)
    }
}

/// Press feedback: the key settles down and an ember wash lights it from
/// the inside for exactly as long as the finger is on it, then eases out.
private struct PasscodeKeyStyle: ButtonStyle {
    func makeBody(configuration: Configuration) -> some View {
        configuration.label
            .overlay(
                Circle()
                    .fill(
                        RadialGradient(
                            colors: [Color.cavnarEmber2.opacity(0.55), Color.cavnarEmber.opacity(0.25)],
                            center: .center, startRadius: 0, endRadius: CavnarPasscodePad.keySize * 0.55
                        )
                    )
                    .opacity(configuration.isPressed ? 1 : 0)
            )
            .shadow(color: Color.cavnarEmber.opacity(configuration.isPressed ? 0.6 : 0), radius: 16)
            .scaleEffect(configuration.isPressed ? 0.92 : 1)
            .animation(configuration.isPressed ? nil : .easeOut(duration: 0.28), value: configuration.isPressed)
    }
}

private extension View {
    /// One key of the keypad's staggered rise-in.
    func keyReveal(_ shown: Bool, index: Int, reduceMotion: Bool) -> some View {
        opacity(shown ? 1 : 0)
            .offset(y: shown ? 0 : 12)
            .animation(reduceMotion ? nil : .easeOut(duration: 0.42).delay(0.04 * Double(index)), value: shown)
    }
}
