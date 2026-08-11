import SwiftUI

struct HomeView: View {
    @Environment(SessionStore.self) private var sessionStore
    @State private var viewModel = HomeViewModel()
    @State private var showingLocationSwitcher = false
    @State private var showingNotifications = false
    @State private var path = NavigationPath()
    // Ticked on every accepted tile/row tap instead of calling Haptic.light()
    // directly in the action closure, paired with .sensoryFeedback below.
    @State private var navHapticTrigger = 0
    @State private var lastNavigationAt = Date.distantPast
    @State private var showDate = false
    @State private var showSubtitle = false

    var body: some View {
        NavigationStack(path: $path) {
            ScrollView {
                VStack(alignment: .leading, spacing: 0) {
                    if let summary = viewModel.summary {
                        // Full-bleed, unpadded — the animated ember background
                        // behind the greeting needs to run edge to edge, not
                        // sit inside the 20pt content margin the rest of the
                        // page uses.
                        hero(summary)
                        VStack(alignment: .leading, spacing: 20) {
                            needsAttentionSection(summary)
                        }
                        .padding(20)
                    } else if viewModel.isLoading {
                        ProgressView().padding(.top, 80).frame(maxWidth: .infinity)
                    } else if let error = viewModel.errorMessage {
                        VStack(spacing: 8) {
                            Text(error).font(.cavnarBody(14)).foregroundStyle(Color.cavnarInk3)
                            Button("Retry") { Task { await viewModel.load() } }
                        }
                        .padding(.top, 80)
                        .frame(maxWidth: .infinity)
                    }
                }
            }
            .navigationDestination(for: ModuleRoute.self) { route in
                ModuleDestinationView(moduleKey: route.key, moduleLabel: route.label)
            }
            .sensoryFeedback(.impact(weight: .light), trigger: navHapticTrigger)
            .background(Color.cavnarPaper)
            .refreshable { await viewModel.load() }
            .navigationTitle("Home")
            .toolbar {
                ToolbarItem(placement: .topBarTrailing) {
                    Button {
                        Haptic.light()
                        showingNotifications = true
                    } label: {
                        Image(systemName: "bell")
                    }
                    // .plain strips the default toolbar-button chrome that
                    // was stacking its own automatic tap feedback on top of
                    // our manual Haptic.light() above — same fix as the FAB
                    // and module tiles, which never had this problem because
                    // they already use a stripped-chrome button style.
                    .buttonStyle(.plain)
                }
                if sessionStore.currentUser?.isOwner == true {
                    ToolbarItem(placement: .topBarTrailing) {
                        Button {
                            Haptic.light()
                            showingLocationSwitcher = true
                        } label: {
                            Image(systemName: "building.2")
                        }
                        .buttonStyle(.plain)
                    }
                }
            }
            .sheet(isPresented: $showingLocationSwitcher) {
                LocationSwitcherView { Task { await viewModel.load() } }
            }
            .sheet(isPresented: $showingNotifications) {
                NotificationsListView()
            }
            .task { await viewModel.load() }
        }
    }

    // Mirrors the web dashboard's Home hero (templates/dashboard.html,
    // #home-tw-headline/#home-tw-date/#home-tw-sub): an ember date eyebrow,
    // then "{name} — your restaurant is running on AI." with the name in
    // ember, then a subtitle line — each stage fading/typing in after the
    // previous finishes, over the animated ember background.
    private func hero(_ summary: HomeSummary) -> some View {
        VStack(spacing: 10) {
            Text(todayDateString)
                .font(.cavnarBody(11, weight: 700))
                .tracking(2)
                .textCase(.uppercase)
                .foregroundStyle(Color.cavnarEmber)
                .opacity(showDate ? 1 : 0)

            HeroHeadlineText(
                name: greetingName(summary),
                rest: "— your restaurant is running on AI."
            ) {
                withAnimation(.easeOut(duration: 0.35)) { showSubtitle = true }
            }
            .opacity(showDate ? 1 : 0)

            if showSubtitle {
                subtitleText(summary)
                    .transition(.opacity)
            }
        }
        .frame(maxWidth: .infinity)
        .multilineTextAlignment(.center)
        .padding(.vertical, 28)
        .padding(.horizontal, 20)
        .background(HomeHeroBackground())
        .onAppear {
            withAnimation(.easeOut(duration: 0.3).delay(0.2)) { showDate = true }
        }
    }

    private func greetingName(_ summary: HomeSummary) -> String {
        guard let username = summary.username, !username.isEmpty else { return "Welcome back" }
        return username.prefix(1).uppercased() + username.dropFirst()
    }

    private var todayDateString: String {
        let formatter = DateFormatter()
        formatter.dateFormat = "MMM d, yyyy"
        formatter.locale = Locale(identifier: "en_US")
        return formatter.string(from: Date()).uppercased()
    }

    private func subtitleText(_ summary: HomeSummary) -> some View {
        var text = Text(summary.restaurantName).foregroundStyle(Color.cavnarInk3)
        if let locationName = summary.locationName, !locationName.isEmpty {
            text = text + Text(" — \(locationName)").foregroundStyle(Color.cavnarInk3)
        }
        if summary.reviewsAwaitingApproval > 0 {
            let n = summary.reviewsAwaitingApproval
            text = text
                + Text("   ·   ").foregroundStyle(Color.cavnarInk3)
                + Text("\(n) review\(n == 1 ? "" : "s") awaiting approval")
                    .foregroundStyle(Color.cavnarEmber)
                    .fontWeight(.semibold)
        }
        return text.font(.cavnarBody(13))
    }

    @ViewBuilder
    private func needsAttentionSection(_ summary: HomeSummary) -> some View {
        VStack(alignment: .leading, spacing: 10) {
            Text("Needs attention")
                .font(.cavnarBody(13, weight: 700))
                .foregroundStyle(Color.cavnarInk)

            if summary.needsAttention.isEmpty {
                AllClearRow().cavnarCard()
            } else {
                VStack(spacing: 8) {
                    ForEach(summary.needsAttention) { item in
                        Button {
                            navigate(to: ModuleRoute(key: item.module, label: item.module.capitalized))
                        } label: {
                            NeedsAttentionRow(item: item)
                        }
                        .buttonStyle(.plain)
                    }
                }
                .cavnarCard()
            }
        }
    }

    // A Button inside a ScrollView/LazyVGrid has to let the ScrollView's own
    // pan gesture "race" its tap gesture to tell a scroll from a tap — under
    // a fast swipe-off-one-tile-and-tap-another, that disambiguation can
    // resolve the FIRST tile's tap late, after the second tile's tap has
    // already gone through, landing a stale extra navigation (and haptic)
    // moments after the real one. Rather than trust the timing of Button's
    // action closure at all, ignore any tap that lands within 350ms of the
    // last one we accepted — short enough that no legitimate back-to-back
    // navigation reads as "the same gesture settling twice," long enough to
    // absorb the stale-resolution window this class of bug produces.
    private func navigate(to route: ModuleRoute) {
        let now = Date()
        guard now.timeIntervalSince(lastNavigationAt) > 0.35 else { return }
        lastNavigationAt = now
        navHapticTrigger += 1
        path.append(route)
    }
}

/// Word-by-word reveal for the hero greeting, same pacing as TypewriterText,
/// but split across two differently-colored halves ("{name}" in ember, the
/// rest in ink) — TypewriterText itself is single-color, so this is a
/// dedicated one-off for the one place in the app that needs two-tone reveal
/// plus a completion callback to chain the subtitle's own fade-in after it.
private struct HeroHeadlineText: View {
    let name: String
    let rest: String
    var onComplete: (() -> Void)?

    @State private var visibleWordCount = 0

    private var nameWords: [String] { name.split(separator: " ").map(String.init) }
    private var restWords: [String] { rest.split(separator: " ").map(String.init) }
    private var allWords: [String] { nameWords + restWords }

    var body: some View {
        allWords.prefix(visibleWordCount).enumerated()
            .reduce(Text("")) { partial, item in
                let (index, word) = item
                let piece = Text((index > 0 ? " " : "") + word)
                    .foregroundStyle(index < nameWords.count ? Color.cavnarEmber : Color.cavnarInk)
                return partial + piece
            }
            .font(.cavnarHeadline(26))
            .lineSpacing(3)
            .task(id: "\(name)|\(rest)") {
                visibleWordCount = 0
                let total = allWords.count
                guard total > 0 else { onComplete?(); return }
                let delayNanos = UInt64(min(max(1400.0 / Double(total), 16), 55) * 1_000_000)
                for i in 1...total {
                    try? await Task.sleep(nanoseconds: delayNanos)
                    if Task.isCancelled { return }
                    visibleWordCount = i
                }
                onComplete?()
            }
    }
}
