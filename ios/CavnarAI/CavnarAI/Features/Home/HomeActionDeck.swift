import SwiftUI

/// Needs Attention: the one thing to tap, then everything else, all of it
/// visible (friction audit #11 / U3-17, 9/25/26).
///
/// The lead card carries a real call to action ("Publish 3 replies") and an
/// optional second link ("Read them first"). Every other item is a one-line
/// row under it with its own action, so nothing waits behind a swipe — the
/// old deck showed one card and hid "time off waiting" behind it, and an
/// owner could leave thinking they had cleared everything. The first four
/// show (the web's focus card plus three rows, and exactly what the server
/// logs as shown); "+N more" opens the rest in place.
struct HomeActionDeck: View {
    let items: [NeedsAttentionItem]
    /// "Start here" when this deck leads Home; "Then these" under the one
    /// thing, so the page has one "Start here", not two (density #3).
    var title: String = "Start here"
    var busy: Bool = false
    let onPrimary: (NeedsAttentionItem) -> Void
    let onSecondary: (NeedsAttentionItem) -> Void
    /// "snooze" (not today) or "recommendation" (hide two weeks) on an item
    /// the server marked dismissable.
    var onDismiss: ((NeedsAttentionItem, String) -> Void)? = nil

    /// Shown before "+N more": the lead plus three rows, as on the web.
    static let shownByDefault = 4

    @State private var showingAll = false

    // 156, not 150: the buttons along the bottom are 44pt targets now.
    static let cardHeight: CGFloat = 156

    /// One height for the lead card: the base, plus room for the evidence
    /// line and the confidence line when the item carries them (K4).
    static func cardHeight(for items: [NeedsAttentionItem]) -> CGFloat {
        cardHeight
            + (items.contains { $0.evidenceLine != nil } ? 36 : 0)
            + (items.contains { $0.confidence != nil } ? 26 : 0)
    }

    /// The rows under the lead, and how many more wait behind "+N more".
    static func rows(_ items: [NeedsAttentionItem], showingAll: Bool) -> (rows: [NeedsAttentionItem], hidden: Int) {
        let rest = Array(items.dropFirst())
        guard !showingAll else { return (rest, 0) }
        let shown = Array(rest.prefix(shownByDefault - 1))
        return (shown, rest.count - shown.count)
    }

    var body: some View {
        VStack(alignment: .leading, spacing: 12) {
            HomeSectionHeader(kicker: "Needs attention", title: title,
                              trailing: items.count > 1 ? "\(items.count) open" : nil)

            if let lead = items.first {
                ActionDeckCard(
                    item: lead,
                    height: Self.cardHeight(for: [lead]),
                    busy: busy,
                    onPrimary: { onPrimary(lead) },
                    onSecondary: { onSecondary(lead) },
                    onDismiss: onDismiss.map { f -> (String) -> Void in { kind in f(lead, kind) } }
                )
            }

            let split = Self.rows(items, showingAll: showingAll)
            if !split.rows.isEmpty {
                VStack(spacing: 0) {
                    ForEach(Array(split.rows.enumerated()), id: \.element.id) { index, item in
                        if index > 0 {
                            Rectangle().fill(Color.cavnarPaper3).frame(height: 1)
                        }
                        ActionDeckRow(
                            item: item,
                            busy: busy && item.isPublishAction,
                            onPrimary: { onPrimary(item) },
                            onDismiss: onDismiss.map { f -> (String) -> Void in { kind in f(item, kind) } }
                        )
                    }
                }
                .padding(.horizontal, 14)
                .background(Color.cavnarPaper2.opacity(0.85))
                .overlay(RoundedRectangle(cornerRadius: CavnarRadius.card, style: .continuous)
                    .strokeBorder(Color.cavnarPaper3, lineWidth: 1))
                .clipShape(RoundedRectangle(cornerRadius: CavnarRadius.card, style: .continuous))
            }

            if split.hidden > 0 {
                Button {
                    Haptic.selection()
                    withAnimation(.easeOut(duration: 0.25)) { showingAll = true }
                } label: {
                    Text("+\(split.hidden) more")
                        .font(.cavnarNumber(13.5, weight: 700))
                        .foregroundStyle(Color.cavnarEmber2)
                        .frame(maxWidth: .infinity, minHeight: 44)
                        .contentShape(Rectangle())
                }
                .buttonStyle(.plain)
                .accessibilityLabel("Show \(split.hidden) more")
            }
        }
        // A publish that cleared the list shouldn't leave "+N more" open
        // over a list that no longer has more.
        .onChange(of: items.count) { _, count in
            if count <= Self.shownByDefault { showingAll = false }
        }
    }
}

/// One item after the lead: its title on one line, the detail under it,
/// and its action as a text button at the right — a 44pt target. The same
/// "Not today / hide" answers the lead card has, from a long press.
private struct ActionDeckRow: View {
    let item: NeedsAttentionItem
    let busy: Bool
    let onPrimary: () -> Void
    var onDismiss: ((String) -> Void)? = nil

    var body: some View {
        HStack(alignment: .center, spacing: 12) {
            VStack(alignment: .leading, spacing: 2) {
                HomeMixedText.make(item.title, size: 14.5, weight: 700, color: .cavnarInk)
                    .lineLimit(1)
                HomeMixedText.make(item.detail, size: 12.5, weight: 500, color: .cavnarInk3)
                    .lineLimit(1)
            }
            Spacer(minLength: 8)
            if let cta = item.cta {
                Button(action: onPrimary) {
                    Group {
                        if busy {
                            CavnarShimmerText(text: "Working…")
                        } else {
                            HStack(spacing: 3) {
                                HomeMixedText.make(cta, size: 13, weight: 800, color: .cavnarEmber2,
                                                   numberWeight: 700, numberColor: .cavnarEmber2)
                                Image(systemName: "chevron.right")
                                    .font(.system(size: 9, weight: .bold))
                                    .foregroundStyle(Color.cavnarEmber2)
                            }
                        }
                    }
                    .frame(minHeight: 44)
                    .contentShape(Rectangle())
                }
                .buttonStyle(.plain)
                .disabled(busy)
            }
        }
        .padding(.vertical, 6)
        .contextMenu {
            if item.dismissable == true, let onDismiss {
                Button("Not today") { onDismiss("snooze") }
                Button("Hide for two weeks") { onDismiss("recommendation") }
            }
        }
    }
}

/// One card in the deck: the ember tile for the item's type, title and
/// detail, and the two actions along the bottom edge. Obsidian with an
/// ember lit edge and a soft cast — branded, not a wall of orange.
private struct ActionDeckCard: View {
    let item: NeedsAttentionItem
    var height: CGFloat = HomeActionDeck.cardHeight
    let busy: Bool
    let onPrimary: () -> Void
    let onSecondary: () -> Void
    var onDismiss: ((String) -> Void)? = nil

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
                    // What it rests on, inline (K4) — and how sure, with
                    // "Why?" behind it. Compact: the reason and any
                    // caution are in the sheet, so the deck keeps one
                    // height.
                    if let evidence = item.evidenceLine {
                        HomeMixedText.make(evidence, size: 12.5, weight: 500, color: .cavnarInk3)
                            .lineLimit(2)
                    }
                    if let c = item.confidence {
                        ConfidenceLine(confidence: c, recKey: item.recKey, surface: "home",
                                       module: "home", compact: true)
                    }
                }
                Spacer(minLength: 0)
                // The same answers a recommendation has — never on a
                // critical item (a guest waiting, a broken sync).
                if item.dismissable == true, let onDismiss {
                    Menu {
                        Button("Not today") { onDismiss("snooze") }
                        Button("Hide for two weeks") { onDismiss("recommendation") }
                    } label: {
                        Image(systemName: "ellipsis")
                            .font(.system(size: 14, weight: .bold))
                            .foregroundStyle(Color.cavnarInk3)
                            .frame(width: 44, height: 44)
                            .contentShape(Rectangle())
                    }
                    .accessibilityLabel("Not today, or hide")
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
                        .frame(minHeight: 44)
                        .contentShape(Rectangle())
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
        // A floor, not a fixed height: at the largest Dynamic Type sizes the
        // title and detail grow and a fixed height clipped the CTA row off
        // the card (F3-18). At default sizes it is the same 156pt card.
        .frame(minHeight: height)
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
            // 44pt tall at card scale (friction audit #50).
            .frame(minHeight: 44)
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
