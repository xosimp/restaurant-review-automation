import SwiftUI

/// The employee's whole app. Deliberately not a stripped-down dashboard —
/// it is a different product, and nothing owner-facing can appear here
/// because the owner routes are unreachable with a staff token
/// (auth._console_denied) rather than merely hidden.
///
/// The frame (employee audit M1 / Part C, wave 2 I2): a bottom tab bar —
/// Today · Tasks · Requests · Me — on the owner app's true-black chrome,
/// with a red count on Requests for what waits on this person. A TabView
/// keeps every tab alive, so leaving Requests mid-edit loses nothing and
/// coming back doesn't refetch (UX-12). All state lives in one
/// StaffPortalStore handed to each tab (`store:`); see its CONTRACT.
struct StaffPortalView: View {
    @Environment(StaffSessionStore.self) private var staff
    @State private var store = StaffPortalStore()
    /// I1's hand-off (Push/StaffDeepLink.swift): a staff push tapped while
    /// the app was anywhere waits there until the portal takes it.
    @State private var deepLinks = StaffDeepLinkCenter.shared

    var body: some View {
        @Bindable var store = store
        TabView(selection: $store.selectedTab) {
            StaffTodayView(store: store)
                .tabItem { Label(StaffTab.today.portalTitle, systemImage: StaffTab.today.portalSymbol) }
                .tag(StaffTab.today)

            StaffTasksTab(store: store)
                .tabItem { Label(StaffTab.tasks.portalTitle, systemImage: StaffTab.tasks.portalSymbol) }
                .tag(StaffTab.tasks)

            StaffRequestsTab(store: store)
                .tabItem { Label(StaffTab.requests.portalTitle, systemImage: StaffTab.requests.portalSymbol) }
                // The DS badge rule: a red count is for what needs this
                // person — swaps asked of them and shifts offered them.
                .badge(store.requestsBadge)
                .tag(StaffTab.requests)

            StaffMeView(store: store)
                .tabItem { Label(StaffTab.me.portalTitle, systemImage: StaffTab.me.portalSymbol) }
                .tag(StaffTab.me)
        }
        .sensoryFeedback(.selection, trigger: store.selectedTab) { _, _ in AppPreferences.hapticsEnabledSnapshot }
        // The owner app's chrome (RootView.mainTabs): true black under the
        // tab bar, the warm near-black page above it.
        .toolbarBackground(Color.cavnarChrome, for: .tabBar)
        .toolbarBackground(.visible, for: .tabBar)
        .task {
            store.attach(staff)
            consumeDeepLink()
            await store.refreshAll()
        }
        // Back in the foreground after five minutes away: everything again
        // (H6) — a week republished or a swap answered while the phone sat
        // in a pocket shows up without a pull.
        .refreshOnForeground(olderThan: 300, lastLoaded: store.foregroundClock) {
            await store.refreshAll()
        }
        .onChange(of: deepLinks.pending) { _, new in
            if new != nil { consumeDeepLink() }
        }
        .onReceive(NotificationCenter.default.publisher(for: .cavnarStaffDeepLink)) { _ in
            consumeDeepLink()
        }
        .onChange(of: store.selectedTab) { old, new in
            // Leaving Requests (an answer may have moved the count) or
            // arriving there: the badge catches up.
            if old == .requests || new == .requests {
                Task { await store.reloadBadges() }
            }
        }
        .sheet(isPresented: $store.showingInbox, onDismiss: {
            Task { await store.reloadBadges() }
        }) {
            StaffInboxView(store: store)
        }
        .sheet(isPresented: $store.showingMessages, onDismiss: {
            store.messageShiftDate = nil
            Task { await store.reloadBadges() }
        }) {
            StaffMessageThreadView(store: store, shiftDate: store.messageShiftDate)
        }
    }

    private func consumeDeepLink() {
        guard let link = deepLinks.consume() else { return }
        store.apply(link)
    }

    /// The first day in the week with a shift, and how to say when it is:
    /// "Today", "Tomorrow" (the day after today in the list), or the
    /// weekday. Nil when every day is off. Clock-free; Today's hero uses
    /// the clock-aware StaffTodayPlan.hero(week:now:).
    static func nextShift(_ week: [StaffWeekDay]) -> (day: StaffWeekDay, when: String)? {
        guard let i = week.firstIndex(where: { !$0.legs.isEmpty }) else { return nil }
        let day = week[i]
        if day.isToday { return (day, "Today") }
        if i > 0, week[i - 1].isToday { return (day, "Tomorrow") }
        return (day, day.weekday)
    }
}

// MARK: - Tab frames

/// Each tab's ground: the page colour, a scroll view with the house
/// gutter, and ember pull-to-refresh (motion #15).
struct StaffTabScroll<Content: View>: View {
    var refresh: (() async -> Void)?
    @ViewBuilder var content: () -> Content

    var body: some View {
        ZStack {
            Color.cavnarPaper.ignoresSafeArea()
            ScrollView {
                VStack(alignment: .leading, spacing: 14) {
                    content()
                }
                .padding(.horizontal, 20)
                .padding(.top, 12)
                .padding(.bottom, 40)
                .frame(maxWidth: .infinity, alignment: .leading)
            }
            .modifier(StaffOptionalRefresh(action: refresh))
        }
    }
}

private struct StaffOptionalRefresh: ViewModifier {
    let action: (() async -> Void)?

    @ViewBuilder
    func body(content: Content) -> some View {
        if let action {
            content.cavnarEmberRefreshable(action)
        } else {
            content
        }
    }
}

/// Tasks: I4's StaffTaskSheetsSection on the store's tasks, with a failed
/// load said as a failure and retried, never an endless pulse (C7).
private struct StaffTasksTab: View {
    let store: StaffPortalStore

    var body: some View {
        StaffTabScroll(refresh: { await store.reloadTasks() }) {
            StaffScreenTitle(title: "Tasks")
            switch store.tasks.phase {
            case .ready:
                if let tasks = store.tasks.value {
                    StaffTaskSheetsSection(response: tasks) {
                        await store.reloadTasks()
                    }
                }
            case .failed:
                StaffLoadFailed(what: "your sheets", message: store.tasks.error) {
                    await store.reloadTasks()
                }
            case .loading:
                StaffLoadingLine(text: "Loading your sheets")
            }
        }
    }
}

/// Requests: I3's StaffRequestsView (it loads its own lists). A pull
/// rebuilds it, which reloads them, and catches the badge up; a tab
/// switch never does (it stays mounted, UX-12).
private struct StaffRequestsTab: View {
    let store: StaffPortalStore
    @State private var generation = 0

    var body: some View {
        StaffTabScroll(refresh: {
            generation += 1
            await store.reloadBadges()
        }) {
            StaffScreenTitle(title: "Requests")
            StaffRequestsView()
                .id(generation)
        }
    }
}

// MARK: - Shared pieces (the staff screens' kit)

/// A tab's title: Clash at the section size, a header for VoiceOver.
struct StaffScreenTitle: View {
    let title: String

    var body: some View {
        Text(title)
            .font(.cavnarHeadline(CavnarType.section))
            .foregroundStyle(Color.cavnarInk)
            .accessibilityAddTraits(.isHeader)
    }
}

/// An uppercase tracked kicker in ink3 (never ember inside a card, DS §4),
/// marked as a header so the rotor can jump between sections (UX-18).
struct StaffKicker: View {
    let text: String
    @Environment(\.colorSchemeContrast) private var contrast

    var body: some View {
        Text(text.uppercased())
            .font(.cavnarBody(CavnarType.kicker, weight: 700))
            .kerning(1.3)
            .foregroundStyle(Color.cavnarInk3(contrast))
            .accessibilityAddTraits(.isHeader)
    }
}

/// The house loading state (DESIGN_SYSTEM §10): the sliding ember line
/// with a plain label under it — never a spinner, never "…".
struct StaffLoadingLine: View {
    let text: String

    var body: some View {
        VStack(alignment: .leading, spacing: 8) {
            CavnarSkeletonBar(height: 3).frame(width: 180)
            Text(text)
                .font(.cavnarBody(CavnarType.secondary))
                .foregroundStyle(Color.cavnarInk3)
        }
        .frame(maxWidth: .infinity, alignment: .leading)
        .accessibilityElement(children: .combine)
    }
}

/// A load that failed: one plain red sentence and Try again — never an
/// empty list standing in for "we don't know" (C7, UX-07).
struct StaffLoadFailed: View {
    let what: String
    var message: String?
    let retry: () async -> Void
    @State private var retrying = false

    var body: some View {
        VStack(alignment: .leading, spacing: 6) {
            Text("Couldn\u{2019}t load \(what). \(message ?? "")".trimmingCharacters(in: .whitespaces))
                .font(.cavnarBody(CavnarType.secondary, weight: 600))
                .foregroundStyle(Color.cavnarRed)
                .fixedSize(horizontal: false, vertical: true)
            Button {
                Task {
                    retrying = true
                    await retry()
                    retrying = false
                }
            } label: {
                VStack(alignment: .leading, spacing: 4) {
                    Text("Try again")
                        .font(.cavnarBody(CavnarType.body, weight: 700))
                        .foregroundStyle(Color.cavnarEmber2)
                    if retrying { CavnarSkeletonBar(height: 3).frame(width: 72) }
                }
                .frame(minHeight: 44, alignment: .leading)
                .contentShape(Rectangle())
            }
            .buttonStyle(.plain)
            .disabled(retrying)
        }
        .frame(maxWidth: .infinity, alignment: .leading)
    }
}

/// A staggered rise for rows on first load (CavnarInteractions'
/// cavnarRowEntrance), and nothing at all under Reduce Motion — that
/// entrance has no still fallback of its own.
struct StaffRise: ViewModifier {
    let index: Int
    let clock: CavnarEntranceClock
    @Environment(\.accessibilityReduceMotion) private var reduceMotion

    @ViewBuilder
    func body(content: Content) -> some View {
        if reduceMotion {
            content
        } else {
            content.cavnarRowEntrance(index: index, clock: clock)
        }
    }
}

extension View {
    func staffRise(_ index: Int, _ clock: CavnarEntranceClock) -> some View {
        modifier(StaffRise(index: index, clock: clock))
    }
}
