import SwiftUI

/// Asks the host to show the command sheet. A request made before the
/// host exists (a cold launch from a shortcut, or behind the Face ID lock —
/// the host lives inside the unlocked tabs) waits until it appears.
@MainActor
enum CommandSheetRequest {
    static let notification = Notification.Name("cavnarOpenCommandSheet")
    private static var pending = false

    static func request() {
        pending = true
        NotificationCenter.default.post(name: notification, object: nil)
    }

    /// True once per request.
    static func take() -> Bool {
        defer { pending = false }
        return pending
    }
}

private struct CommandSheetHost: ViewModifier {
    @State private var showing = false

    func body(content: Content) -> some View {
        content
            .onAppear { if CommandSheetRequest.take() { showing = true } }
            .onReceive(NotificationCenter.default.publisher(for: CommandSheetRequest.notification)) { _ in
                if CommandSheetRequest.take() { showing = true }
            }
            .sheet(isPresented: $showing) {
                CommandSheet()
                    .presentationDetents([.large])
                    .presentationDragIndicator(.visible)
            }
    }
}

extension View {
    /// Hosts the command sheet. Attach once, inside the unlocked app — the
    /// lock screen replaces this whole tree, so the sheet can never show
    /// over it.
    func cavnarCommandSheetHost() -> some View {
        modifier(CommandSheetHost())
    }
}

/// Find or ask anything (Friction audit #47). A bottom sheet, the field at
/// the bottom above the keyboard: what's waiting first, then one-tap
/// places and locations; typing matches places, people and items, and the
/// last row always hands the text to Ask Cavnar.
struct CommandSheet: View {
    @State private var viewModel = CommandSheetViewModel()
    @State private var locations = LocationSwitcherViewModel()
    @State private var personKey: PersonSheetTarget?
    @FocusState private var fieldFocused: Bool
    @Environment(\.dismiss) private var dismiss
    @Environment(SessionStore.self) private var sessionStore
    @Environment(DeepLinkRouter.self) private var router

    var body: some View {
        NavigationStack {
            ScrollView {
                VStack(alignment: .leading, spacing: 22) {
                    if viewModel.trimmedQuery.isEmpty {
                        oneTap
                        waitingSection
                        locationSection
                    } else {
                        typedSection
                    }
                }
                .padding(.horizontal, 20)
                .padding(.vertical, 16)
            }
            .scrollDismissesKeyboard(.interactively)
            .safeAreaInset(edge: .bottom) { field }
            .accountSheetChrome("Find or ask")
        }
        .task {
            locations.session = sessionStore
            async let a: Void = viewModel.load()
            async let b: Void = locations.load()
            _ = await (a, b)
        }
        .onChange(of: viewModel.query) { _, _ in viewModel.queryChanged() }
        .sheet(item: $personKey) { target in
            PersonSheet(target: target)
        }
    }

    // MARK: Field

    private var field: some View {
        HStack(spacing: 10) {
            Image(systemName: "magnifyingglass")
                .font(.system(size: 15, weight: .semibold))
                .foregroundStyle(Color.cavnarInk3)
            TextField("Find or ask anything…", text: $viewModel.query)
                .font(.cavnarBody(16))
                .foregroundStyle(Color.cavnarInk)
                .focused($fieldFocused)
                .submitLabel(.go)
                .autocorrectionDisabled()
                .onSubmit { submit() }
            if !viewModel.query.isEmpty {
                Button {
                    viewModel.query = ""
                } label: {
                    Image(systemName: "xmark.circle.fill")
                        .font(.system(size: 16))
                        .foregroundStyle(Color.cavnarInk3)
                        .frame(width: 44, height: 44)
                        .contentShape(Rectangle())
                }
                .buttonStyle(.plain)
                .accessibilityLabel("Clear")
            }
        }
        .padding(.leading, 14)
        .frame(minHeight: 48)
        .background(Color.cavnarPaper2, in: RoundedRectangle(cornerRadius: CavnarRadius.control))
        .overlay(RoundedRectangle(cornerRadius: CavnarRadius.control)
            .strokeBorder(Color.cavnarEmber.opacity(fieldFocused ? 0.6 : 0.2), lineWidth: 1))
        .padding(.horizontal, 16)
        .padding(.vertical, 10)
        .background(Color.cavnarPaper)
    }

    /// Return opens the best match; with no match it asks Cavnar. It never
    /// confirms an action — that is always a tap on the card.
    private func submit() {
        if let first = viewModel.matchedCommands.first, first.kind == "nav", let nav = first.nav {
            go(nav)
        } else if let hit = viewModel.searchResults.first {
            open(hit)
        } else {
            ask(viewModel.trimmedQuery)
        }
    }

    // MARK: Empty state: the day's list

    private var oneTap: some View {
        VStack(alignment: .leading, spacing: 10) {
            AccountKicker(text: "One tap")
            ScrollView(.horizontal, showsIndicators: false) {
                HStack(spacing: 8) {
                    if let replies = viewModel.waiting.first(where: { $0.key == "no_response" }), (replies.count ?? 0) > 0 {
                        chip("Replies (\(replies.count ?? 0))", icon: "checkmark.bubble") { go("reviews?filter=pending") }
                    }
                    chip("Last night", icon: "chart.bar.doc.horizontal") { go("dsr") }
                    chip("Scan invoice", icon: "doc.text.viewfinder") { go("inventory/invoices?scan=camera") }
                    chip("Requests", icon: "person.2") { go("labor/requests") }
                    chip("Ask Cavnar", icon: "sparkles") { go("ask") }
                }
            }
        }
    }

    private func chip(_ label: String, icon: String, action: @escaping () -> Void) -> some View {
        Button {
            Haptic.light()
            action()
        } label: {
            HStack(spacing: 6) {
                Image(systemName: icon).font(.system(size: 12, weight: .semibold))
                HomeMixedText.make(label, size: 14, weight: 600, color: .cavnarInk)
            }
            .foregroundStyle(Color.cavnarEmber2)
            .padding(.horizontal, 14)
            .frame(minHeight: 44)
            .background(Color.cavnarPaper2, in: Capsule())
            .overlay(Capsule().strokeBorder(Color.cavnarPaper3, lineWidth: 1))
        }
        .buttonStyle(.plain)
    }

    @ViewBuilder
    private var waitingSection: some View {
        VStack(alignment: .leading, spacing: 10) {
            AccountKicker(text: "Waiting on you")
            if viewModel.isLoading && viewModel.waiting.isEmpty && viewModel.pendingSends.isEmpty {
                CavnarSkeletonLines(widths: [1.0, 0.8, 0.6])
            } else if viewModel.waiting.isEmpty && viewModel.pendingSends.isEmpty {
                Text("Nothing waiting on you.")
                    .font(.cavnarBody(14))
                    .foregroundStyle(Color.cavnarInk3)
            } else {
                VStack(spacing: 8) {
                    ForEach(viewModel.pendingSends, id: \.id) { send in
                        pendingSendRow(send)
                    }
                    ForEach(viewModel.waiting) { item in
                        waitingRow(item)
                    }
                }
            }
        }
    }

    private func pendingSendRow(_ send: PendingSendActivities.PendingAction) -> some View {
        let key = "send:\(send.id)"
        let busy = viewModel.rowBusy.contains(key)
        let title = send.label?.isEmpty == false ? send.label! : PendingSendAttributes.plainTitle(kind: send.kind)
        return HStack(alignment: .center, spacing: 10) {
            VStack(alignment: .leading, spacing: 3) {
                HomeMixedText.make(title, size: 15, weight: 700, color: .cavnarInk)
                if let outcome = viewModel.rowOutcome[key] {
                    Text(outcome).font(.cavnarBody(13)).foregroundStyle(Color.cavnarInk3)
                } else if let fire = CavnarISODate.parse(send.executeAt) {
                    (Text("Goes out in ").font(.cavnarBody(13))
                     + Text(fire, style: .relative).font(.cavnarNumber(13, weight: 600)))
                        .foregroundStyle(Color.cavnarInk3)
                }
            }
            Spacer(minLength: 6)
            if viewModel.rowOutcome[key] == nil {
                Button {
                    Haptic.light()
                    Task { await viewModel.undo(send) }
                } label: {
                    Group {
                        if busy { CavnarShimmerText(text: "Undoing…") } else { Text("Undo") }
                    }
                }
                .buttonStyle(CavnarSecondaryButtonStyle())
                .disabled(busy)
            }
        }
        .padding(12)
        .background(Color.white.opacity(0.03))
        .clipShape(RoundedRectangle(cornerRadius: 12, style: .continuous))
    }

    private func waitingRow(_ item: CommandWaitingItem) -> some View {
        let busy = viewModel.rowBusy.contains(item.key)
        return VStack(alignment: .leading, spacing: 8) {
            Button {
                Haptic.light()
                if let path = item.destination { go(path.raw) }
            } label: {
                HStack(alignment: .top, spacing: 10) {
                    Circle()
                        .fill(item.severity == "critical" ? Color.cavnarRed
                              : (item.severity == "important" ? Color.cavnarAmber : Color.cavnarInk3))
                        .frame(width: 7, height: 7)
                        .padding(.top, 6)
                    VStack(alignment: .leading, spacing: 3) {
                        HomeMixedText.make(item.title, size: 15, weight: 700, color: .cavnarInk)
                            .fixedSize(horizontal: false, vertical: true)
                        if let outcome = viewModel.rowOutcome[item.key] {
                            Text(outcome).font(.cavnarBody(13, weight: 600)).foregroundStyle(Color.cavnarGreen)
                        } else if let detail = item.detail {
                            HomeMixedText.make(detail, size: 13, color: .cavnarInk3)
                        }
                    }
                    Spacer(minLength: 0)
                    Image(systemName: "chevron.right")
                        .font(.system(size: 11, weight: .bold))
                        .foregroundStyle(Color.cavnarInk3)
                        .padding(.top, 5)
                }
                .contentShape(Rectangle())
            }
            .buttonStyle(.plain)
            if item.request != nil, viewModel.rowOutcome[item.key] == nil {
                HStack(spacing: 10) {
                    Button {
                        Haptic.light()
                        Task { await viewModel.decide(item, approve: false) }
                    } label: { Text("Deny").frame(maxWidth: .infinity) }
                    .buttonStyle(CavnarSecondaryButtonStyle())
                    .disabled(busy)
                    Button {
                        Haptic.light()
                        Task { await viewModel.decide(item, approve: true) }
                    } label: {
                        Group {
                            if busy { CavnarShimmerText(text: "Deciding…") } else { Text("Approve") }
                        }
                        .frame(maxWidth: .infinity)
                    }
                    .buttonStyle(CavnarSecondaryButtonStyle())
                    .disabled(busy)
                }
            }
        }
        .padding(12)
        .background(Color.white.opacity(0.03))
        .clipShape(RoundedRectangle(cornerRadius: 12, style: .continuous))
    }

    @ViewBuilder
    private var locationSection: some View {
        if locations.locations.count > 1 {
            VStack(alignment: .leading, spacing: 10) {
                AccountKicker(text: "Locations")
                ScrollView(.horizontal, showsIndicators: false) {
                    HStack(spacing: 8) {
                        ForEach(locations.locations) { loc in
                            let current = loc.id == sessionStore.currentUser?.restaurantId
                            chip(current ? "\(loc.name) ✓" : loc.name, icon: "building.2") {
                                guard !current else { return }
                                switchTo(loc)
                            }
                        }
                    }
                }
            }
        }
    }

    // MARK: Typed

    @ViewBuilder
    private var typedSection: some View {
        if let proposal = viewModel.proposal {
            VStack(alignment: .leading, spacing: 10) {
                AccountKicker(text: "Confirm")
                ProposalCard(proposal: proposal, viewModel: viewModel.askViewModel)
                Button("Back to results") { viewModel.clearProposal() }
                    .font(.cavnarBody(14, weight: 600))
                    .foregroundStyle(Color.cavnarEmber2)
                    .frame(minHeight: 44)
            }
        } else {
            let commands = viewModel.matchedCommands
            if !commands.isEmpty {
                VStack(alignment: .leading, spacing: 6) {
                    AccountKicker(text: "Go to or do")
                    ForEach(commands) { entry in commandRow(entry) }
                }
            }
            if viewModel.isSearching && viewModel.searchResults.isEmpty {
                CavnarSkeletonLines(widths: [1.0, 0.7])
            } else if !viewModel.searchResults.isEmpty {
                VStack(alignment: .leading, spacing: 6) {
                    AccountKicker(text: "Found")
                    ForEach(viewModel.searchResults) { hit in resultRow(hit) }
                }
            }
            if let error = viewModel.errorLine {
                Text(error).font(.cavnarBody(14)).foregroundStyle(Color.cavnarRed)
            }
            askRow
        }
    }

    private func commandRow(_ entry: CommandEntry) -> some View {
        Button {
            Haptic.light()
            run(entry)
        } label: {
            row(icon: entry.kind == "action" ? "bolt.fill" : (entry.kind == "ask" ? "sparkles" : "arrow.up.right"),
                title: entry.label,
                subtitle: entry.kind == "action" ? (entry.tier >= 2 ? "Shows what goes out before anything is sent" : "You confirm first") : nil,
                busy: viewModel.proposing == entry.id)
        }
        .buttonStyle(.plain)
        .disabled(viewModel.proposing != nil)
    }

    private func resultRow(_ hit: CommandSearchResult) -> some View {
        Button {
            Haptic.light()
            open(hit)
        } label: {
            let place = hit.location.map { $0.id == sessionStore.currentUser?.restaurantId ? nil : $0.name } ?? nil
            let sub = [hit.subtitle, place].compactMap { $0 }.joined(separator: " · ")
            row(icon: hit.systemImage, title: hit.title, subtitle: sub.isEmpty ? nil : sub, busy: false)
        }
        .buttonStyle(.plain)
    }

    private var askRow: some View {
        Button {
            Haptic.light()
            ask(viewModel.trimmedQuery)
        } label: {
            row(icon: "sparkles", title: "Ask Cavnar AI: “\(viewModel.trimmedQuery)”", subtitle: nil, busy: false,
                tint: .cavnarEmber2)
        }
        .buttonStyle(.plain)
    }

    private func row(icon: String, title: String, subtitle: String?, busy: Bool, tint: Color = .cavnarInk) -> some View {
        HStack(spacing: 12) {
            Image(systemName: icon)
                .font(.system(size: 14, weight: .semibold))
                .foregroundStyle(Color.cavnarEmber)
                .frame(width: 22)
            VStack(alignment: .leading, spacing: 2) {
                if busy {
                    CavnarShimmerText(text: "Preparing…")
                } else {
                    HomeMixedText.make(title, size: 15, weight: 600, color: tint)
                        .lineLimit(2)
                }
                if let subtitle {
                    HomeMixedText.make(subtitle, size: 12.5, color: .cavnarInk3)
                        .lineLimit(1)
                }
            }
            Spacer(minLength: 0)
        }
        .padding(.vertical, 8)
        .frame(minHeight: 44)
        .contentShape(Rectangle())
    }

    // MARK: Doing

    private func run(_ entry: CommandEntry) {
        switch entry.kind {
        case "action":
            Task { await viewModel.propose(entry) }
        case "ask":
            ask(viewModel.trimmedQuery.isEmpty ? entry.label : viewModel.trimmedQuery)
        default:
            if let nav = entry.nav { go(nav) }
        }
    }

    private func open(_ hit: CommandSearchResult) {
        if let key = hit.personKey {
            personKey = PersonSheetTarget(key: key, name: hit.title)
            return
        }
        // A hit from another location switches there first — the router's
        // own rule for a push about another store.
        if let loc = hit.location, let current = sessionStore.currentUser?.restaurantId, loc.id != current {
            let nav = hit.nav
            dismiss()
            Task {
                // SessionStore reports the switch; RootView resets.
                if let switchLocation = router.switchLocation { _ = await switchLocation(loc.id) }
                if let nav { SystemEntry.open(NavPath(nav) ?? NavPath("home")!) }
            }
            return
        }
        if let nav = hit.nav { go(nav) }
    }

    private func ask(_ text: String) {
        guard let path = SystemEntry.askPath(text) else { return }
        go(path.raw)
    }

    /// Close the sheet, then send the app there — the router does the rest.
    private func go(_ raw: String) {
        guard let path = NavPath(raw) else { return }
        dismiss()
        Task { @MainActor in
            try? await Task.sleep(for: .milliseconds(250))
            SystemEntry.open(path)
        }
    }

    private func switchTo(_ loc: LocationOption) {
        dismiss()
        Task {
            if let switchLocation = router.switchLocation { _ = await switchLocation(loc.id) }
        }
    }
}
