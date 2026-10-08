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
    /// An iPad on the host stand (parity audit #99): a regular width puts
    /// the four tabs in a sidebar; a compact one keeps the tab bar.
    @Environment(\.horizontalSizeClass) private var sizeClass

    var body: some View {
        Group {
            if CavnarLayout.usesSidebar(sizeClass) {
                splitShell
            } else {
                tabShell
            }
        }
        .background { keyboardShortcuts }
        .modifier(StaffPortalLifecycle(store: store, staff: staff, deepLinks: deepLinks,
                                       consumeDeepLink: consumeDeepLink))
    }

    /// The phone's bar: Today · Tasks · Requests · Me.
    private var tabShell: some View {
        @Bindable var store = store
        return TabView(selection: $store.selectedTab) {
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
        // The owner app's chrome (RootView.mainTabs): true black under the
        // tab bar, the warm near-black page above it.
        .toolbarBackground(Color.cavnarChrome, for: .tabBar)
        .toolbarBackground(.visible, for: .tabBar)
    }

    /// The same four tabs in a sidebar, the selected one beside it. The
    /// store is the one state, so a push or the inbox lands the same way.
    private var splitShell: some View {
        NavigationSplitView {
            List(selection: Binding<StaffTab?>(get: { store.selectedTab },
                                               set: { if let tab = $0 { store.selectedTab = tab } })) {
                ForEach(StaffTab.bar, id: \.self) { tab in
                    Label {
                        Text(tab.portalTitle)
                            .font(.cavnarBody(15, weight: 600))
                            .foregroundStyle(Color.cavnarInk)
                    } icon: {
                        Image(systemName: tab.portalSymbol)
                            .font(.system(size: 15, weight: .semibold))
                            .foregroundStyle(Color.cavnarEmber)
                    }
                    .badge(tab == .requests ? store.requestsBadge : 0)
                    .tag(tab)
                }
            }
            .listStyle(.sidebar)
            .scrollContentBackground(.hidden)
            .background(Color.cavnarChrome.ignoresSafeArea())
            .navigationTitle("Cavnar AI")
            .navigationBarTitleDisplayMode(.inline)
            .toolbar { cavnarTitleToolbar("Cavnar AI") }
            .navigationSplitViewColumnWidth(min: 220, ideal: 250, max: 300)
        } detail: {
            // Each tab draws its own title (StaffScreenTitle); the column's
            // bar only carries the sidebar button, on the page colour.
            Group {
                switch store.selectedTab {
                case .tasks: StaffTasksTab(store: store)
                case .requests: StaffRequestsTab(store: store)
                case .me: StaffMeView(store: store)
                default: StaffTodayView(store: store)
                }
            }
            .cavnarReadableWidth()
            .frame(maxWidth: .infinity, maxHeight: .infinity)
            .background(Color.cavnarPaper.ignoresSafeArea())
            .toolbarBackground(Color.cavnarPaper, for: .navigationBar)
        }
        .navigationSplitViewStyle(.balanced)
    }

    /// ⌘1…⌘4 for the four tabs, ⌘R for the one on screen.
    private var keyboardShortcuts: some View {
        ZStack {
            ForEach(Array(StaffTab.bar.enumerated()), id: \.offset) { index, tab in
                CavnarShortcutButton(title: tab.portalTitle,
                                     key: KeyEquivalent(Character(String(index + 1)))) {
                    store.selectedTab = tab
                }
            }
            CavnarShortcutButton(title: "Refresh", key: "r") {
                NotificationCenter.default.post(name: CavnarKeyCommand.refresh, object: nil)
            }
        }
    }
}

/// The portal's loads, deep links, foreground rule and sheets — one chain
/// for both shapes (the tab bar and the iPad sidebar).
private struct StaffPortalLifecycle: ViewModifier {
    @Bindable var store: StaffPortalStore
    let staff: StaffSessionStore
    let deepLinks: StaffDeepLinkCenter
    let consumeDeepLink: () -> Void

    func body(content: Content) -> some View {
        content
        .sensoryFeedback(.selection, trigger: store.selectedTab) { _, _ in AppPreferences.hapticsEnabledSnapshot }
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
                .cavnarFormSheet()
        }
        .sheet(isPresented: $store.showingMessages, onDismiss: {
            store.messageShiftDate = nil
            Task { await store.reloadBadges() }
        }) {
            StaffMessageThreadView(store: store, shiftDate: store.messageShiftDate)
                .cavnarFormSheet()
        }
    }
}

extension StaffPortalView {
    fileprivate func consumeDeepLink() {
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

/// Tasks: I4's StaffTaskSheetsSection, which reads /tasks itself (the one
/// read path: version 2, last night's note, the "as of" copy, the offline
/// queue) and says its own loading and failed states (C7). A pull forces a
/// read.
private struct StaffTasksTab: View {
    let store: StaffPortalStore

    var body: some View {
        StaffTabScroll(refresh: { await store.reloadTasks() }) {
            StaffScreenTitle(title: "Tasks")
            StaffTaskSheetsSection()
        }
    }
}

/// Requests: I3's StaffRequestsView (it loads its own lists). A pull bumps
/// `refresh`, which reloads them, and catches the badge up; a tab switch
/// never does (it stays mounted, UX-12). With `portal`, every answer
/// reloads the badges and the week.
private struct StaffRequestsTab: View {
    let store: StaffPortalStore
    @State private var generation = 0

    var body: some View {
        ScrollViewReader { proxy in
            StaffTabScroll(refresh: {
                generation += 1
                await store.reloadBadges()
            }) {
                StaffScreenTitle(title: "Requests")
                StaffRequestsView(refresh: generation, portal: store) { id in
                    withAnimation(.easeOut(duration: 0.3)) { proxy.scrollTo(id, anchor: .center) }
                }
            }
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
