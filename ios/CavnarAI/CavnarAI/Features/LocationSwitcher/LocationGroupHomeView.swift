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
                        ForEach(g.locations) { loc in
                            locationCard(loc)
                        }
                    }
                    VStack(alignment: .leading, spacing: 12) {
                        HomeSectionHeader(kicker: "Benchmarks", title: "How your locations compare")
                        LocationComparisonSection()
                    }
                } else if viewModel.isLoading {
                    CavnarSkeletonLines(widths: [0.5, 0.9, 0.75, 0.6], lineHeight: 14, spacing: 12)
                } else {
                    Text(viewModel.errorMessage ?? "Couldn\u{2019}t load all locations.")
                        .font(.cavnarBody(15))
                        .foregroundStyle(Color.cavnarInk2)
                }
                if let error = viewModel.errorMessage, viewModel.group != nil {
                    Text(error).font(.cavnarBody(14, weight: 600)).foregroundStyle(Color.cavnarRed)
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
            Text(("All locations" + (g.groupName.map { " \u{00B7} " + $0 } ?? "")).uppercased())
                .font(.cavnarBody(CavnarType.kicker, weight: 700))
                .tracking(1.6)
                .foregroundStyle(Color.cavnarEmber2)
            if let headline = g.headline {
                Text(headline)
                    .font(.cavnarHeadline(24))
                    .foregroundStyle(HomeView.briefToneColor(g.tone))
                    .fixedSize(horizontal: false, vertical: true)
            }
            if let line = g.portfolio?.line {
                HomeMixedText.make(line, size: 14, weight: 600, color: .cavnarInk2)
                    .fixedSize(horizontal: false, vertical: true)
                    .cavnarSensitive()
            }
        }
    }

    // MARK: Needs attention, every location

    private func attentionCard(_ g: LocationGroupBrief) -> some View {
        let all = g.attention
        let shown = showAllAttention ? all : Array(all.prefix(Self.attentionShown))
        return VStack(alignment: .leading, spacing: 12) {
            HomeSectionHeader(kicker: "Needs attention", title: "Every location",
                              trailing: all.isEmpty ? nil : "\(all.count)")
            VStack(alignment: .leading, spacing: 0) {
                if let sentence = g.summaryLine {
                    HomeMixedText.make(sentence, size: 13.5, weight: 600, color: .cavnarInk2)
                        .fixedSize(horizontal: false, vertical: true)
                        .padding(.bottom, 8)
                }
                if all.isEmpty {
                    Text("Nothing needs you at any location.")
                        .font(.cavnarBody(14, weight: 600))
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
                            .font(.cavnarBody(13, weight: 700))
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
                    HomeMixedText.make(a.text, size: 14.5, weight: 600, color: .cavnarInk)
                        .fixedSize(horizontal: false, vertical: true)
                    if let loc = a.location {
                        Text(loc).font(.cavnarBody(12.5, weight: 600)).foregroundStyle(Color.cavnarInk3)
                    }
                }
                Spacer(minLength: 8)
                Text(a.actionLabel ?? "Open")
                    .font(.cavnarBody(13, weight: 700))
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
        let labor = LocationGroupFormat.labor(loc.labor)
        let reviews = LocationGroupFormat.reviews(loc.reviews)
        let food = LocationGroupFormat.foodCost(loc.inventory)
        return Button {
            Haptic.selection()
            switchTo(loc)
        } label: {
            VStack(alignment: .leading, spacing: 12) {
                HStack(alignment: .center, spacing: 8) {
                    Circle().fill(LocationSwitcherView.healthColor(loc.health)).frame(width: 9, height: 9)
                    Text(loc.name)
                        .font(.cavnarHeadline(18))
                        .foregroundStyle(Color.cavnarInk)
                        .lineLimit(1)
                    if loc.active {
                        Text("viewing")
                            .font(.cavnarBody(12, weight: 600))
                            .foregroundStyle(Color.cavnarInk3)
                    }
                    Spacer(minLength: 4)
                    if switching == loc.id {
                        CavnarSkeletonBar(height: 3).frame(width: 40)
                    } else {
                        Image(systemName: "chevron.right")
                            .font(.system(size: 12, weight: .bold))
                            .foregroundStyle(Color.cavnarInk3)
                    }
                }
                if let first = loc.issues.first {
                    HomeMixedText.make(first.text + (loc.issues.count > 1 ? "  +\(loc.issues.count - 1)" : ""),
                                       size: 13, weight: 600,
                                       color: LocationSwitcherView.healthColor(first.severity))
                        .fixedSize(horizontal: false, vertical: true)
                }
                LazyVGrid(columns: [GridItem(.flexible(), spacing: 10), GridItem(.flexible(), spacing: 10)],
                          alignment: .leading, spacing: 10) {
                    cell("Last night", night)
                    cell("Labor", labor)
                    cell("Rating \u{00B7} 30 days", reviews)
                    cell("Food cost", food)
                }
                HStack(spacing: 6) {
                    Text("Last active")
                        .font(.cavnarBody(12, weight: 600))
                        .foregroundStyle(Color.cavnarInk3)
                    HomeMixedText.make(LocationGroupFormat.lastActive(loc.lastActive), size: 12, weight: 600,
                                       color: .cavnarInk2)
                }
            }
            .cavnarSensitive()
            .frame(maxWidth: .infinity, alignment: .leading)
            .cavnarCard()
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

    private func cell(_ label: String, _ value: (figure: String, detail: String?)) -> some View {
        VStack(alignment: .leading, spacing: 3) {
            Text(label.uppercased())
                .font(.cavnarBody(10.5, weight: 700))
                .tracking(1.1)
                .foregroundStyle(Color.cavnarInk3)
                .lineLimit(1)
            Text(value.figure)
                .font(.cavnarNumber(19, weight: 700))
                .foregroundStyle(value.figure == LocationGroupFormat.dash ? Color.cavnarInk3 : Color.cavnarInk)
                .lineLimit(1)
                .minimumScaleFactor(0.8)
            if let detail = value.detail {
                HomeMixedText.make(detail, size: 12, weight: 500, color: .cavnarInk3)
                    .lineLimit(2)
            }
        }
        .frame(maxWidth: .infinity, alignment: .leading)
        .padding(10)
        .background(Color.cavnarPaper2.opacity(0.6), in: RoundedRectangle(cornerRadius: 12, style: .continuous))
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
