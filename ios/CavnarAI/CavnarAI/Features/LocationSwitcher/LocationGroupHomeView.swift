import SwiftUI

/// All locations, on the phone (parity audit #32): the web's group Home —
/// the group's last night in one strip, everything that needs the owner at
/// any location (every item, three then "+N more"), and one card per
/// location with last night against budget, labor %, the 30-day rating
/// with urgent and waiting replies, the food-cost opportunity and when
/// someone last signed in there. Numbers are never averaged across
/// locations; each stays on its own card. A missing figure is "—".
///
/// Tap a card to switch to that location; long-press for its places.
/// Same body as the web: GET /mobile/api/home/brief/group.
struct LocationGroupHomeView: View {
    let viewModel: LocationSwitcherViewModel
    var onSwitched: () -> Void
    /// Closes the whole locations sheet (this screen is pushed inside it).
    var close: () -> Void
    @Environment(SessionStore.self) private var session
    @Environment(DeepLinkRouter.self) private var router
    @State private var showAllAttention = false
    @State private var switching: Int?

    /// Rows past the first three wait behind "+N more" (the web's rule).
    static let attentionShown = 3

    var body: some View {
        ScrollView {
            VStack(alignment: .leading, spacing: 26) {
                if let g = viewModel.group {
                    header(g)
                    attentionCard(g)
                    VStack(alignment: .leading, spacing: 12) {
                        HomeSectionHeader(kicker: "Side by side", title: "Your locations",
                                          trailing: "\(g.locations.count)")
                        // One line per location (iOS re-audit M15): the dot,
                        // the name, last night's net and the one issue —
                        // the four-figure grid per card is the web's.
                        VStack(spacing: 0) {
                            ForEach(Array(g.locations.enumerated()), id: \.element.id) { index, loc in
                                locationCard(loc)
                                if index < g.locations.count - 1 { AccountRowDivider() }
                            }
                        }
                        .cavnarCard()
                    }
                    // "Web explains. iPhone decides." — the location-to-
                    // location comparison is analysis (M15).
                    CavnarWebLinkRow(title: "How your locations compare",
                                     subtitle: "Labor, food cost, reviews and sales, location against location",
                                     path: "locations", actionLabel: "Open on the web")
                } else if viewModel.isLoading {
                    CavnarSkeletonLines(widths: [0.5, 0.9, 0.75, 0.6], lineHeight: 14, spacing: 12)
                } else {
                    Text(viewModel.errorMessage ?? "Couldn\u{2019}t load all locations.")
                        .font(.cavnar(.body))
                        .foregroundStyle(Color.cavnarInk2)
                }
                if let error = viewModel.errorMessage, viewModel.group != nil {
                    Text(error).font(.cavnarBody(CavnarType.secondary, weight: 600)).foregroundStyle(Color.cavnarRedText)
                }
            }
            .padding(20)
        }
        .cavnarEmberRefreshable { await viewModel.loadGroup(fresh: true) }
        .cavnarModuleBackground()
        .navigationTitle("")
        .navigationBarTitleDisplayMode(.inline)
        .toolbar { cavnarTitleToolbar("All locations") }
    }

    // MARK: Header

    private func header(_ g: LocationGroupBrief) -> some View {
        VStack(alignment: .leading, spacing: 8) {
            CavnarKicker("All locations" + (g.groupName.map { " \u{00B7} " + $0 } ?? ""))
            if let headline = g.headline {
                Text(headline)
                    .font(.cavnar(.title))
                    .foregroundStyle(HomeView.briefToneColor(g.tone))
                    .fixedSize(horizontal: false, vertical: true)
            }
            if let line = g.portfolio?.line {
                HomeMixedText.make(line, size: CavnarType.secondary, weight: 600, color: .cavnarInk2)
                    .fixedSize(horizontal: false, vertical: true)
                    .cavnarSensitive()
            }
        }
    }

    // MARK: Needs you, every location

    private func attentionCard(_ g: LocationGroupBrief) -> some View {
        let all = g.attention
        let shown = showAllAttention ? all : Array(all.prefix(Self.attentionShown))
        return VStack(alignment: .leading, spacing: 12) {
            HomeSectionHeader(kicker: "Needs you", title: "Every location",
                              trailing: all.isEmpty ? nil : "\(all.count)")
            VStack(alignment: .leading, spacing: 0) {
                if let sentence = g.summaryLine {
                    HomeMixedText.make(sentence, size: CavnarType.caption, weight: 600, color: .cavnarInk2)
                        .fixedSize(horizontal: false, vertical: true)
                        .padding(.bottom, 8)
                }
                if all.isEmpty {
                    Text("Nothing needs you at any location.")
                        .font(.cavnarBody(CavnarType.secondary, weight: 600))
                        .foregroundStyle(Color.cavnarGreen)
                        .padding(.vertical, 6)
                }
                ForEach(Array(shown.enumerated()), id: \.element.id) { index, a in
                    attentionRow(a)
                    if index < shown.count - 1 { AccountRowDivider() }
                }
                if all.count > Self.attentionShown {
                    Button {
                        Haptic.light()
                        withAnimation(.easeOut(duration: 0.2)) { showAllAttention.toggle() }
                    } label: {
                        Text(showAllAttention ? "Show fewer" : "+\(all.count - Self.attentionShown) more")
                            .font(.cavnarBody(CavnarType.caption, weight: 700))
                            .foregroundStyle(Color.cavnarEmber2)
                            .frame(minHeight: 44, alignment: .leading)
                            .contentShape(Rectangle())
                    }
                    .buttonStyle(.plain)
                }
            }
            .cavnarCard()
        }
    }

    private func attentionRow(_ a: LocationGroupBrief.Attention) -> some View {
        Button {
            Haptic.light()
            guard let rid = a.restaurantId, let nav = NavPath(a.nav ?? "home") else { return }
            open(nav, at: rid)
        } label: {
            HStack(alignment: .top, spacing: 10) {
                Circle().fill(LocationSwitcherView.healthColor(a.severity)).frame(width: 8, height: 8).padding(.top, 6)
                VStack(alignment: .leading, spacing: 2) {
                    HomeMixedText.make(a.text, size: CavnarType.secondary, weight: 600, color: .cavnarInk)
                        .fixedSize(horizontal: false, vertical: true)
                    if let loc = a.location {
                        Text(loc).font(.cavnarBody(CavnarType.caption, weight: 600)).foregroundStyle(Color.cavnarInk3)
                    }
                }
                Spacer(minLength: 8)
                Text(a.actionLabel ?? "Open")
                    .font(.cavnarBody(CavnarType.caption, weight: 700))
                    .foregroundStyle(Color.cavnarEmber2)
            }
            .padding(.vertical, 8)
            .frame(minHeight: 44)
            .contentShape(Rectangle())
        }
        .buttonStyle(.plain)
    }

    // MARK: One card per location

    private func locationCard(_ loc: LocationGroupBrief.Location) -> some View {
        let night = LocationGroupFormat.lastNight(loc.lastNight)
        return Button {
            Haptic.selection()
            switchTo(loc)
        } label: {
            HStack(alignment: .top, spacing: CavnarSpace.s) {
                Circle().fill(LocationSwitcherView.healthColor(loc.health)).frame(width: 9, height: 9)
                    .padding(.top, 6)
                    .accessibilityHidden(true)
                VStack(alignment: .leading, spacing: 2) {
                    HStack(alignment: .firstTextBaseline, spacing: CavnarSpace.xs) {
                        Text(loc.name)
                            .cavnarText(.label)
                            .lineLimit(2)
                        if loc.active {
                            Text("viewing").cavnarText(.caption, color: .cavnarInk2)
                        }
                        Spacer(minLength: 4)
                        HomeMixedText.make(night.figure, role: .label,
                                           color: night.figure == LocationGroupFormat.dash ? .cavnarInk2 : .cavnarInk)
                            .accessibilityLabel("Last night \(night.figure)")
                    }
                    // The one issue, in its own tone (red text for a red one).
                    if let first = loc.issues.first {
                        CavnarMixedText(first.text + (loc.issues.count > 1 ? "  +\(loc.issues.count - 1)" : ""),
                                        role: .secondary,
                                        color: first.severity == "critical" ? .cavnarRedText : .cavnarInk2)
                            .lineLimit(2)
                    }
                }
                if switching == loc.id {
                    CavnarSkeletonBar(height: 3).frame(width: 40)
                } else {
                    Image(systemName: "chevron.right")
                        .font(.cavnar(.caption))
                        .foregroundStyle(Color.cavnarInk2)
                        .padding(.top, 4)
                        .accessibilityHidden(true)
                }
            }
            .padding(.vertical, CavnarSpace.s)
            .cavnarSensitive()
            .frame(maxWidth: .infinity, minHeight: 44, alignment: .leading)
            .contentShape(Rectangle())
        }
        .buttonStyle(.plain)
        .disabled(switching != nil)
        .accessibilityHint("Switches to this location")
        .contextMenu {
            Button { switchTo(loc) } label: {
                Label(loc.active ? "Back to its Home" : "Switch to \(loc.name)", systemImage: "building.2")
            }
            Button { open(NavPath("dsr")!, at: loc.id) } label: {
                Label("Last night\u{2019}s report", systemImage: "chart.bar.doc.horizontal")
            }
            Button {
                open(NavPath((loc.reviews?.urgent ?? 0) > 0 ? "reviews?filter=urgent" : "reviews?filter=pending")!, at: loc.id)
            } label: { Label("Its reviews", systemImage: "star.bubble") }
            if loc.labor != nil {
                Button { open(NavPath("labor")!, at: loc.id) } label: { Label("Its labor", systemImage: "person.2") }
            }
            if loc.inventory != nil {
                Button { open(NavPath("inventory")!, at: loc.id) } label: { Label("Its food cost", systemImage: "shippingbox") }
            }
        }
    }



    // MARK: Actions

    private func switchTo(_ loc: LocationGroupBrief.Location) {
        guard switching == nil else { return }
        guard let option = viewModel.locations.first(where: { $0.id == loc.id }) else { return }
        if option.active {
            close()
            return
        }
        switching = loc.id
        Task {
            viewModel.session = session
            if await viewModel.switchTo(option) {
                onSwitched()
                close()
            }
            switching = nil
        }
    }

    /// A place at another location: the router switches there first.
    private func open(_ nav: NavPath, at restaurantId: Int) {
        close()
        router.open(nav, restaurantId: restaurantId)
    }
}
