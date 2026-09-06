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
    /// The id of the card actually being dragged/flown off — not
    /// "whichever card is currently depth 0," which is a moving target the
    /// instant `advance()` updates `index` mid-transition. See advance()'s
    /// comment for the flash this used to cause when the two were conflated.
    @State private var draggingID: String?
    /// The id of a card that's about to become the front card WITHOUT
    /// having been visible as a ghost first — only ever the "previous"
    /// item, since `stack` only ever peeks forward (index, index+1,
    /// index+2). See advance()'s comment for why "next" doesn't need this
    /// and "previous" does.
    @State private var enteringID: String?
    @State private var enterOffset: CGFloat = 0

    static let cardHeight: CGFloat = 150
    private static let ghostStep: CGFloat = 14
    /// How far off the leading edge a "previous" card starts before it
    /// slides into the front position — wide enough to be genuinely off
    /// screen on every device, so its insertion fade happens unseen.
    private static let slideIn: CGFloat = 460

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
                    .offset(
                        x: (entry.id == draggingID ? dragX : 0) + (entry.id == enteringID ? enterOffset : 0),
                        y: CGFloat(entry.depth) * Self.ghostStep
                    )
                    .rotationEffect(.degrees(entry.id == draggingID ? Double(dragX) / 28 : 0), anchor: .bottom)
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
                // Kept in sync with the actual current front card on every
                // tick, not just once — so a fresh drag starting right
                // after a previous advance() always targets whichever card
                // is really on top now, with no explicit reset needed
                // in between (see advance()'s comment).
                draggingID = stack.first?.id
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
    ///
    /// The card that was flying off used to be identified by depth (`entry
    /// .depth == 0`), which is a POSITION, not the card itself — the
    /// instant `index` advances below, the card that had been showing at
    /// depth 1 (offset x: 0 the whole time) becomes depth 0 too, and reads
    /// dragX for that one frame before it's separately reset. Getting that
    /// reset and the index change into perfectly the same commit relied on
    /// two back-to-back but separate transactions (one with animations
    /// disabled, one animated) landing as a single render — timing that
    /// wasn't guaranteed, and the miss is exactly the flash-then-snap that
    /// was reported. Tracking `draggingID` — the ACTUAL card being
    /// dragged — instead of a depth means the incoming card never reads
    /// dragX at all, in any frame, so there's nothing left to race.
    ///
    /// That fix made "next" (swipe left) smooth but left "previous" (swipe
    /// right) just as choppy, for a completely separate reason: `stack`
    /// only ever peeks FORWARD (index, index+1, index+2). Going next, the
    /// incoming card was already on screen as a ghost with a well-defined
    /// smaller/dimmer starting state to grow from. Going previous, the
    /// incoming card (index-1) was never part of the stack at all — it
    /// doesn't exist in the tree until `index` changes, so it just pops in
    /// via the plain .opacity insertion transition instead of animating
    /// into place, which is the "snappy" feel. Giving it a starting
    /// offset (enterOffset, off to the left) that resolves to 0 in the
    /// SAME transaction as the index change turns that pop into a slide,
    /// mirroring the outgoing card's own fly-off. Only needed when that
    /// card genuinely wasn't already visible — for 2-3 total items the
    /// "previous" card overlaps with an existing ghost, and forcing an
    /// artificial slide onto an already-positioned ghost would itself be
    /// the same kind of jump this exists to prevent.
    private func advance(_ direction: Int) {
        guard count > 1, !flying else { return }
        flying = true
        if draggingID == nil { draggingID = stack.first?.id }
        Haptic.selection()

        guard direction < 0 else {
            // NEXT — unchanged, and confirmed good on device. The front
            // card flies off in the swipe's direction and genuinely LEAVES
            // the deck (for 4+ items its id is no longer in `stack`), so it
            // fades out where it is while the ghost behind grows forward.
            withAnimation(.easeIn(duration: 0.22)) { dragX = -520 }
            Task { @MainActor in
                try? await Task.sleep(for: .milliseconds(220))
                withAnimation(.spring(response: 0.42, dampingFraction: 0.86)) {
                    index = (index + 1) % count
                }
                flying = false
            }
            return
        }

        // PREVIOUS — deliberately NOT a mirror of the above, because the
        // situation isn't mirrored. Going back, the card leaving the front
        // does NOT leave the deck: with `stack` peeking forward, index-1
        // becomes the new front and the old front lands at depth 1, still
        // on screen. Flinging it to +520 therefore stranded it off-screen
        // at a dragX nothing ever reset, and it snapped back into place on
        // the next touch — that's the "brings the card behind it to the
        // front almost like a double render" that was reported. So instead
        // it settles back to centre as it demotes, and the incoming card
        // slides in over the top.
        let incoming = items[(index - 1 + count) % count]
        if !stack.contains(where: { $0.item.id == incoming.id }) {
            enteringID = incoming.id
            enterOffset = -Self.slideIn
        }
        withAnimation(.spring(response: 0.42, dampingFraction: 0.86)) {
            dragX = 0
            index = (index - 1 + count) % count
        }
        Task { @MainActor in
            // One real frame with the incoming card mounted AT its offset
            // before animating it home. A freshly-inserted view has no
            // previous frame to interpolate from, so setting the offset and
            // animating it away inside the same transaction that inserts
            // the card animates nothing at all — which is exactly why the
            // first attempt at this changed nothing on device.
            try? await Task.sleep(for: .milliseconds(16))
            withAnimation(.spring(response: 0.46, dampingFraction: 0.84)) { enterOffset = 0 }
            try? await Task.sleep(for: .milliseconds(420))
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
                        // A dot jump never carries a drag offset — clear
                        // both so a stale draggingID from an earlier swipe
                        // can't reapply if it happens to land back on the
                        // same card the dots just jumped to.
                        draggingID = nil
                        dragX = 0
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
