import SwiftUI

/// Needs Attention as a stacked deck led by the one thing to tap. The top
/// card carries a real call to action ("Publish 3 replies") and an optional
/// second link ("Read them first"); the next two items sit behind it as
/// smaller, dimmer ghosts. Swipe the top card either way (or tap a dot) to
/// bring the next one forward. Replaces the old equal-cards carousel —
/// same needs_attention data, but the screen now says what to do, not
/// just what's wrong.
struct HomeActionDeck: View {
    let items: [NeedsAttentionItem]
    var busy: Bool = false
    let onPrimary: (NeedsAttentionItem) -> Void
    let onSecondary: (NeedsAttentionItem) -> Void

    @State private var index = 0
    @State private var dragX: CGFloat = 0
    @State private var flying = false

    static let cardHeight: CGFloat = 150
    private static let ghostStep: CGFloat = 14

    private struct Entry: Identifiable {
        let depth: Int
        let item: NeedsAttentionItem
        var id: String { item.id }
    }

    private var count: Int { items.count }

    private var stack: [Entry] {
        guard count > 0 else { return [] }
        return (0..<min(3, count)).map { Entry(depth: $0, item: items[(index + $0) % count]) }
    }

    var body: some View {
        VStack(alignment: .leading, spacing: 12) {
            HomeSectionHeader(kicker: "Needs attention", title: "Start here",
                              trailing: count > 1 ? "\(index + 1) of \(count)" : nil)

            ZStack(alignment: .top) {
                // Reversed so the top card (depth 0) is drawn last, on top.
                ForEach(stack.reversed()) { entry in
                    ActionDeckCard(
                        item: entry.item,
                        busy: busy && entry.depth == 0,
                        onPrimary: { onPrimary(entry.item) },
                        onSecondary: { onSecondary(entry.item) }
                    )
                    .scaleEffect(1 - CGFloat(entry.depth) * 0.045, anchor: .bottom)
                    .offset(x: entry.depth == 0 ? dragX : 0, y: CGFloat(entry.depth) * Self.ghostStep)
                    .rotationEffect(.degrees(entry.depth == 0 ? Double(dragX) / 28 : 0), anchor: .bottom)
                    .opacity(entry.depth == 0 ? 1 : (entry.depth == 1 ? 0.72 : 0.42))
                    .saturation(entry.depth == 0 ? 1 : 0.75)
                    .allowsHitTesting(entry.depth == 0 && !flying)
                    .transition(.opacity)
                }
            }
            .frame(maxWidth: .infinity)
            .frame(height: Self.cardHeight + Self.ghostStep * 2)
            .contentShape(Rectangle())
            .gesture(swipe, including: count > 1 ? .all : .subviews)

            if count > 1 {
                dots
            }
        }
        // If the list shrinks under us (a publish just cleared a card),
        // land on a card that still exists.
        .onChange(of: items.map(\.id)) { _, ids in
            if index >= ids.count { index = 0 }
        }
    }

    private var swipe: some Gesture {
        DragGesture(minimumDistance: 16, coordinateSpace: .local)
            .onChanged { value in
                guard !flying else { return }
                // Only follow a mostly-horizontal drag — a vertical one is
                // the page scrolling, and belongs to the ScrollView.
                if abs(value.translation.width) > abs(value.translation.height) {
                    dragX = value.translation.width
                }
            }
            .onEnded { value in
                guard !flying else { return }
                let w = value.translation.width
                if abs(w) > 60, abs(w) > abs(value.translation.height) * 1.2 {
                    advance(w < 0 ? 1 : -1)
                } else {
                    withAnimation(.spring(response: 0.35, dampingFraction: 0.85)) { dragX = 0 }
                }
            }
    }

    /// The top card flies off in the swipe's direction, then the deck
    /// re-stacks around the next item. `direction` 1 = next, -1 = previous.
    private func advance(_ direction: Int) {
        guard count > 1, !flying else { return }
        flying = true
        Haptic.selection()
        withAnimation(.easeIn(duration: 0.22)) { dragX = CGFloat(direction < 0 ? 1 : -1) * 520 }
        Task { @MainActor in
            try? await Task.sleep(for: .milliseconds(220))
            var snap = Transaction()
            snap.disablesAnimations = true
            withTransaction(snap) { dragX = 0 }
            withAnimation(.spring(response: 0.42, dampingFraction: 0.86)) {
                index = (index + direction + count) % count
            }
            flying = false
        }
    }

    private var dots: some View {
        HStack(spacing: 5) {
            ForEach(Array(0..<count), id: \.self) { i in
                Capsule()
                    .fill(i == index ? Color.cavnarEmber2 : Color.cavnarEmber2.opacity(0.35))
                    .frame(width: i == index ? 14 : 5, height: 5)
                    .animation(.easeInOut(duration: 0.25), value: index)
                    .contentShape(Rectangle().size(width: 18, height: 24))
                    .onTapGesture {
                        guard i != index, !flying else { return }
                        Haptic.selection()
                        withAnimation(.spring(response: 0.42, dampingFraction: 0.86)) { index = i }
                    }
            }
        }
        .frame(maxWidth: .infinity)
        .padding(.top, 2)
    }
}

/// One card in the deck: the ember tile for the item's type, title and
/// detail, and the two actions along the bottom edge. Obsidian with an
/// ember lit edge and a soft cast — branded, not a wall of orange.
private struct ActionDeckCard: View {
    let item: NeedsAttentionItem
    let busy: Bool
    let onPrimary: () -> Void
    let onSecondary: () -> Void

    private static let obsidian = Color(red: 0.08, green: 0.08, blue: 0.09)
    private var shape: RoundedRectangle { RoundedRectangle(cornerRadius: 22, style: .continuous) }

    var body: some View {
        VStack(alignment: .leading, spacing: 0) {
            HStack(alignment: .top, spacing: 12) {
                GlowBadge(systemImage: iconName, size: 38)
                VStack(alignment: .leading, spacing: 4) {
                    HomeMixedText.make(item.title, size: 15.5, weight: 700, color: .cavnarInk)
                        .fixedSize(horizontal: false, vertical: true)
                    HomeMixedText.make(item.detail, size: 13, weight: 500, color: .cavnarInk3)
                        .fixedSize(horizontal: false, vertical: true)
                }
            }
            Spacer(minLength: 8)
            HStack {
                if let secondary = item.secondary {
                    Button(action: onSecondary) {
                        HStack(spacing: 3) {
                            Text(secondary).font(.cavnarBody(12, weight: 700))
                            Image(systemName: "chevron.right").font(.system(size: 9, weight: .bold))
                        }
                        .foregroundStyle(Color.cavnarEmber2)
                        .padding(.vertical, 8)
                    }
                    .buttonStyle(.plain)
                }
                Spacer(minLength: 8)
                if let cta = item.cta {
                    Button(action: onPrimary) {
                        if busy {
                            CavnarShimmerText(text: "Working…")
                        } else {
                            HomeMixedText.make(cta, size: 13, weight: 800, color: .white, numberWeight: 700, numberColor: .white)
                        }
                    }
                    .buttonStyle(DeckPrimaryButtonStyle())
                    .disabled(busy)
                }
            }
        }
        .padding(.horizontal, 16)
        .padding(.top, 16)
        .padding(.bottom, 12)
        .frame(maxWidth: .infinity, alignment: .leading)
        .frame(height: HomeActionDeck.cardHeight)
        .background(
            ZStack {
                shape.fill(Self.obsidian)
                shape.fill(
                    LinearGradient(
                        stops: [
                            .init(color: Color.cavnarEmber.opacity(0.26), location: 0),
                            .init(color: Color.cavnarEmber.opacity(0.08), location: 0.6),
                            .init(color: Color.cavnarEmber.opacity(0), location: 1),
                        ],
                        startPoint: .topLeading, endPoint: .bottomTrailing
                    )
                )
            }
        )
        .overlay(shape.strokeBorder(Color.cavnarEmber2.opacity(0.32), lineWidth: 1))
        .clipShape(shape)
        .shadow(color: .black.opacity(0.55), radius: 22, x: 0, y: 14)
        .shadow(color: Color.cavnarEmber.opacity(0.16), radius: 24, x: 0, y: 0)
    }

    private var iconName: String {
        switch item.type {
        case "reviews_awaiting_approval", "urgent_reviews": return "star.fill"
        case "labor_overtime": return "exclamationmark.triangle.fill"
        case "low_response_rate": return "chart.bar.fill"
        default: return "bell.fill"
        }
    }
}

/// The deck's own compact primary button — the app's ember button at card
/// scale (13pt label, 12pt radius) rather than the full 16pt form button.
private struct DeckPrimaryButtonStyle: ButtonStyle {
    func makeBody(configuration: Configuration) -> some View {
        configuration.label
            .padding(.horizontal, 16)
            .padding(.vertical, 11)
            .background(
                RoundedRectangle(cornerRadius: 12, style: .continuous)
                    .fill(LinearGradient(colors: [.cavnarEmber2, .cavnarEmber], startPoint: .top, endPoint: .bottom))
            )
            .overlay(
                RoundedRectangle(cornerRadius: 12, style: .continuous)
                    .strokeBorder(Color.white.opacity(0.14), lineWidth: 1)
            )
            .shadow(color: Color.cavnarEmber.opacity(0.4), radius: 9, x: 0, y: 6)
            .scaleEffect(configuration.isPressed ? 0.97 : 1)
            .animation(.easeOut(duration: 0.12), value: configuration.isPressed)
            .sensoryFeedback(.impact(weight: .medium), trigger: configuration.isPressed) { _, pressed in
                pressed && AppPreferences.hapticsEnabledSnapshot
            }
    }
}
