import SwiftUI

// The Requests tab (employee audit wave 2, I3 — UX-06/07/13/14/34, W2–W5,
// H2/H9/M6/M7): what waits on you first, then your own requests, then one
// primary, "Ask for time off". Availability and preferences moved to Me.
// Same /staff/api routes as the web portal had, one for one; the payloads
// are in StaffRequestModels.swift.

// MARK: - Shared pieces for the staff screens (I3)

enum StaffUI {
    /// A section heading: the Account kicker (ink3, never ember inside the
    /// page — UX-16), and a VoiceOver heading so the rotor can jump (UX-18).
    static func header(_ text: String) -> some View {
        AccountKicker(text: text)
            .padding(.top, 8)
            .accessibilityAddTraits(.isHeader)
    }

    static func note(_ text: String, color: Color = .cavnarInk3) -> some View {
        HomeMixedText.make(text, size: CavnarType.secondary, color: color)
            .fixedSize(horizontal: false, vertical: true)
            .frame(maxWidth: .infinity, alignment: .leading)
    }

    /// A failure, said under the control that failed (DS §7, UX-13).
    static func errorLine(_ text: String) -> some View {
        Text(text)
            .font(.cavnarBody(CavnarType.secondary, weight: 600))
            .foregroundStyle(Color.cavnarRed)
            .fixedSize(horizontal: false, vertical: true)
            .frame(maxWidth: .infinity, alignment: .leading)
            .accessibilityLabel("Error: \(text)")
    }

    /// A section that didn't load: the reason in one red sentence and Try
    /// again — never an empty list, never a form (UX-07, C7).
    static func failedCard(_ message: String, retry: @escaping () -> Void) -> some View {
        VStack(alignment: .leading, spacing: 4) {
            errorLine(message)
            StaffTextButton(title: "Try again", action: retry)
        }
        .frame(maxWidth: .infinity, alignment: .leading)
        .cavnarCard()
    }

    /// The house loading line with a plain label — never "Loading…".
    static func loading(_ label: String) -> some View {
        VStack(alignment: .leading, spacing: 8) {
            CavnarSkeletonBar(height: 3).frame(width: 180)
            Text(label).font(.cavnarBody(CavnarType.secondary)).foregroundStyle(Color.cavnarInk3)
        }
        .frame(maxWidth: .infinity, alignment: .leading)
        .accessibilityElement(children: .combine)
    }
}

/// A button label while its request runs: the verb with the house shimmer,
/// still under Reduce Motion — never "…" (UX-27).
struct StaffBusyLabel: View {
    let text: String
    var color: Color = .white
    @Environment(\.accessibilityReduceMotion) private var reduceMotion

    var body: some View {
        Group {
            if reduceMotion {
                Text(text).foregroundStyle(color.opacity(0.6))
            } else {
                CavnarShimmerText(text: text, color: color)
            }
        }
        .accessibilityLabel(text)
    }
}

/// A text action in a row: ember2, 44pt tall however short the word.
struct StaffTextButton: View {
    let title: String
    var tone: Color = .cavnarEmber2
    var busy: Bool = false
    var busyTitle: String? = nil
    var disabled: Bool = false
    let action: () -> Void

    var body: some View {
        Button {
            action()
        } label: {
            Group {
                if busy { StaffBusyLabel(text: busyTitle ?? title, color: tone) } else { Text(title) }
            }
            .font(.cavnarBody(14.5, weight: 700))
            .foregroundStyle(disabled ? Color.cavnarInk3 : tone)
            .frame(minHeight: 44, alignment: .leading)
            .contentShape(Rectangle())
        }
        .buttonStyle(.plain)
        .disabled(busy || disabled)
    }
}

/// The DS Undo capsule (§12 "Undo toast (iOS)"), shown where the thing was:
/// these screens sit inside the container's scroll view, so the capsule
/// takes the row's place rather than the screen's foot.
struct StaffUndoCapsule: View {
    let text: String
    let undo: () -> Void

    var body: some View {
        HStack(spacing: 12) {
            Text(text)
                .font(.cavnarBody(14.5, weight: 600))
                .foregroundStyle(Color.cavnarInk)
                .lineLimit(2)
            Spacer(minLength: 8)
            Button {
                Haptic.light()
                undo()
            } label: {
                Text("Undo")
                    .font(.cavnarBody(14.5, weight: 700))
                    .foregroundStyle(Color.cavnarEmber2)
                    .frame(minWidth: 44, minHeight: 44)
                    .contentShape(Rectangle())
            }
            .buttonStyle(.plain)
        }
        .padding(.horizontal, 16)
        .background(Color.cavnarPaper2, in: Capsule())
        .overlay(Capsule().strokeBorder(Color.cavnarPaper3, lineWidth: 1))
        .accessibilityElement(children: .contain)
    }
}

extension StaffSessionStore {
    /// A staff GET with a query string. `authed(_:)` puts its path through
    /// appendingPathComponent, which escapes a "?" — so a query has to
    /// travel as `query:`. Same bearer, same sign-out on an ended session.
    func staffGet<Response: Decodable>(_ path: String, query: [String: String]) async throws -> Response {
        guard let token else { throw APIClient.APIError(message: "Not signed in.") }
        do {
            return try await APIClient.shared.sendWithBearer(path, query: query, bearer: token)
        } catch let error as APIClient.SessionExpiredError {
            signOut()
            throw error
        }
    }
}

// MARK: - Bodies (the exact keys the web portal posted)

struct StaffTimeOffBody: Encodable, Equatable {
    let startDate: String
    let endDate: String
    let reason: String
    enum CodingKeys: String, CodingKey {
        case reason
        case startDate = "start_date"
        case endDate = "end_date"
    }
}

struct StaffShiftChangeBody: Encodable, Equatable {
    var kind: String = "drop"
    let date: String
    let shiftStart: String
    var targetName: String? = nil
    var targetDate: String? = nil
    var targetStart: String? = nil
    let reason: String
    enum CodingKeys: String, CodingKey {
        case kind, date, reason
        case shiftStart = "shift_start"
        case targetName = "target_name"
        case targetDate = "target_date"
        case targetStart = "target_start"
    }
}

// MARK: - The Requests tab

/// `StaffRequestsView()` / `StaffRequestsView(refresh:portal:)` — content for
/// the container's scroll view (it does not scroll itself). `refresh`: bump
/// it (pull-to-refresh) to reload. `portal`: the container's store, so an
/// answer catches the Requests and Inbox badges up (`reloadBadges()`) and
/// a change reloads the week (`reloadShifts()`).
struct StaffRequestsView: View {
    var refresh: Int = 0
    var portal: StaffPortalStore?

    init(refresh: Int = 0, portal: StaffPortalStore? = nil) {
        self.refresh = refresh
        self.portal = portal
    }

    @Environment(StaffSessionStore.self) private var staff
    @Environment(\.scenePhase) private var scenePhase
    @State private var board: StaffLoad<StaffRequestsBoard> = .loading
    @State private var timeOff: StaffLoad<[StaffTimeOffRequest]> = .loading
    @State private var refreshNote: String?
    @State private var busy: Set<String> = []
    @State private var rowError: [String: String] = [:]
    @State private var postedLabel: String?
    @State private var confirming: Confirm?
    @State private var pendingUndo: PendingUndo?
    @State private var undoTask: Task<Void, Never>?
    @State private var showingTimeOff = false
    @State private var messaging: MessageSheet?

    /// The tier-2 confirms: what moves, named (UX-14).
    enum Confirm: Identifiable {
        case ask(StaffSwapAsk), offer(StaffShiftOffer), claim(StaffOpenShift)

        var id: String {
            switch self {
            case .ask(let a): return "ask\(a.id)"
            case .offer(let o): return "offer\(o.id)"
            case .claim(let s): return "open\(s.id)"
            }
        }

        var title: String {
            switch self {
            case .ask: return "Accept the swap?"
            case .offer(let o): return "Take \(o.shiftLabel)?"
            case .claim(let s): return "Pick up \(s.shiftLabel)?"
            }
        }

        var message: String {
            switch self {
            case .ask(let a): return a.confirmMessage
            case .offer(let o):
                return "It goes on your schedule\(o.role.map { " as \($0)" } ?? ""), and your manager hears you said yes."
            case .claim(let s):
                return ["It goes on your schedule\(s.role.map { " as \($0)" } ?? "").", s.overtimeNote ?? ""]
                    .filter { !$0.isEmpty }.joined(separator: " ")
            }
        }

        var action: String {
            switch self {
            case .ask: return "Accept the swap"
            case .offer: return "Take it"
            case .claim: return "Pick it up"
            }
        }
    }

    struct PendingUndo: Equatable {
        let key: String
        let label: String
        let path: String
    }

    struct MessageSheet: Identifiable {
        let id = UUID()
        let context: StaffMessageContext?
    }

    var body: some View {
        VStack(alignment: .leading, spacing: 12) {
            waitingSection
            yourRequestsSection
            Button {
                showingTimeOff = true
            } label: {
                Text("Ask for time off").frame(maxWidth: .infinity)
            }
            .buttonStyle(CavnarPrimaryButtonStyle())
            .padding(.top, 10)
        }
        .task { await reload() }
        .onChange(of: refresh) { _, _ in Task { await reload() } }
        .onChange(of: scenePhase) { _, phase in if phase == .active { Task { await reload() } } }
        // Leaving inside the Undo window keeps the request — the safe side.
        .onDisappear { cancelUndo() }
        .sheet(isPresented: $showingTimeOff, onDismiss: { Task { await reload(); await portal?.reloadShifts() } }) {
            StaffTimeOffSheet().environment(staff)
        }
        .sheet(item: $messaging) { sheet in
            Group {
                if let portal {
                    StaffMessageThreadView(store: portal, context: sheet.context)
                } else {
                    StaffMessageThreadView(context: sheet.context)
                }
            }
            .environment(staff)
        }
        .confirmationDialog(confirming?.title ?? "", isPresented: Binding(
            get: { confirming != nil }, set: { if !$0 { confirming = nil } }),
                            titleVisibility: .visible, presenting: confirming) { c in
            Button(c.action) { Task { await perform(c) } }
            Button("Not now", role: .cancel) {}
        } message: { c in
            Text(c.message)
        }
    }

    // MARK: Waiting on you

    @ViewBuilder
    private var waitingSection: some View {
        switch board {
        case .loading:
            StaffUI.header("Waiting on you")
            StaffUI.loading("Loading your shift requests")
        case .failed(let message):
            StaffUI.header("Waiting on you")
            StaffUI.failedCard(message) { Task { await reload() } }
        case .loaded(let b):
            if b.hasWaiting || postedLabel != nil {
                StaffUI.header("Waiting on you")
                if let postedLabel {
                    CavnarInlinePosted(label: postedLabel) { self.postedLabel = nil }
                }
                ForEach(b.asks) { askRow($0) }
                ForEach(b.offers) { offerRow($0) }
                if !b.open.isEmpty {
                    Text("Open shifts")
                        .font(.cavnarBody(CavnarType.body, weight: 700))
                        .foregroundStyle(Color.cavnarInk2)
                        .padding(.top, 4)
                        .accessibilityAddTraits(.isHeader)
                    ForEach(b.open) { openRow($0) }
                }
            }
        }
    }

    private func askRow(_ ask: StaffSwapAsk) -> some View {
        let key = "ask\(ask.id)"
        return VStack(alignment: .leading, spacing: 6) {
            Text("\(ask.employeeName) asked to swap")
                .font(.cavnarBody(CavnarType.body, weight: 700))
                .foregroundStyle(Color.cavnarInk)
            HomeMixedText.make("Their shift: \(ask.theirShift)", size: CavnarType.secondary, color: .cavnarInk2)
            HomeMixedText.make("Your shift: \(ask.yourShift)", size: CavnarType.secondary, color: .cavnarInk2)
            if ask.managerApproved {
                StaffUI.note("Your manager already said yes.")
            }
            HStack(spacing: 10) {
                Button {
                    Task { await act(key, "/staff/api/shift-requests/\(ask.id)/respond",
                                     body: StaffAcceptBody(accept: false), posted: "Declined — \(ask.employeeName) is told") }
                } label: { Text("Decline").frame(maxWidth: .infinity) }
                .buttonStyle(CavnarSecondaryButtonStyle(isDisabled: busy.contains(key)))
                Button {
                    confirming = .ask(ask)
                } label: {
                    Group {
                        if busy.contains(key) { StaffBusyLabel(text: "Answering", color: .cavnarInk) } else { Text("Accept") }
                    }
                    .frame(maxWidth: .infinity)
                }
                .buttonStyle(CavnarSecondaryButtonStyle(isDisabled: busy.contains(key)))
            }
            .disabled(busy.contains(key))
            .padding(.top, 2)
            if let e = rowError[key] { StaffUI.errorLine(e) }
        }
        .frame(maxWidth: .infinity, alignment: .leading)
        .cavnarCard()
        .accessibilityElement(children: .contain)
    }

    private func offerRow(_ offer: StaffShiftOffer) -> some View {
        let key = "offer\(offer.id)"
        return VStack(alignment: .leading, spacing: 6) {
            Text(offer.headline)
                .font(.cavnarBody(CavnarType.body, weight: 700))
                .foregroundStyle(Color.cavnarInk)
            HomeMixedText.make(offer.shiftLabel + (offer.role.map { " · \($0)" } ?? ""),
                               size: CavnarType.secondary, color: .cavnarInk2)
            if let note = offer.note {
                StaffUI.note("\u{201C}\(note)\u{201D}", color: .cavnarInk2)
            }
            if offer.forCoverage {
                StaffUI.note("Your manager needs this one covered.")
            }
            HStack(spacing: 10) {
                Button {
                    Task { await act(key, "/staff/api/offers/\(offer.id)/respond",
                                     body: StaffAcceptBody(accept: false), posted: "Declined — your manager is told") }
                } label: { Text("Decline").frame(maxWidth: .infinity) }
                .buttonStyle(CavnarSecondaryButtonStyle(isDisabled: busy.contains(key)))
                Button {
                    confirming = .offer(offer)
                } label: {
                    Group {
                        if busy.contains(key) { StaffBusyLabel(text: "Answering", color: .cavnarInk) } else { Text("Take it") }
                    }
                    .frame(maxWidth: .infinity)
                }
                .buttonStyle(CavnarSecondaryButtonStyle(isDisabled: busy.contains(key)))
            }
            .disabled(busy.contains(key))
            .padding(.top, 2)
            if let e = rowError[key] { StaffUI.errorLine(e) }
        }
        .frame(maxWidth: .infinity, alignment: .leading)
        .cavnarCard()
        .accessibilityElement(children: .contain)
    }

    private func openRow(_ shift: StaffOpenShift) -> some View {
        let key = "open\(shift.id)"
        return VStack(alignment: .leading, spacing: 4) {
            HomeMixedText.make(shift.shiftLabel + (shift.role.map { " · \($0)" } ?? ""),
                               size: CavnarType.body, weight: 700, color: .cavnarInk)
            Text(shift.postedByManager ? "Posted by your manager"
                                       : (shift.employeeName.map { "\($0) can't work it" } ?? "Open"))
                .font(.cavnarBody(CavnarType.secondary))
                .foregroundStyle(Color.cavnarInk3)
            if let ot = shift.overtimeNote {
                StaffUI.note(ot, color: .cavnarAmber)
            }
            if let why = shift.whyNotLine {
                StaffUI.note(why)
            }
            StaffTextButton(title: "Pick this up", busy: busy.contains(key), busyTitle: "Picking it up",
                            disabled: !shift.takeable) {
                confirming = .claim(shift)
            }
            .accessibilityHint(shift.takeable ? "Puts this shift on your schedule after you confirm." : (shift.whyNotLine ?? ""))
            if let e = rowError[key] { StaffUI.errorLine(e) }
        }
        .frame(maxWidth: .infinity, alignment: .leading)
        .cavnarCard()
        .accessibilityElement(children: .contain)
    }

    // MARK: Your requests

    @ViewBuilder
    private var yourRequestsSection: some View {
        StaffUI.header("Your requests")
        let shifts = board.value?.requests
        let offs = timeOff.value
        if board.isLoading || timeOff.isLoading {
            StaffUI.loading("Loading your requests")
        } else {
            let hidden: Set<String> = pendingUndo.map { [$0.key] } ?? []
            let items = StaffYourRequest.merged(timeOff: offs ?? [], shifts: shifts ?? [], hiding: hidden)
            if let p = pendingUndo {
                StaffUndoCapsule(text: p.label) { cancelUndo() }
            }
            ForEach(items) { item in
                switch item {
                case .timeOff(let t): timeOffRow(t)
                case .shift(let s): shiftRow(s)
                }
            }
            if let f = timeOff.failure {
                StaffUI.failedCard("Your time off didn't load. \(f)") { Task { await reload() } }
            } else if board.failure != nil, offs != nil {
                StaffUI.note("Your shift changes didn't load — Try again above.", color: .cavnarRed)
            }
            if let refreshNote {
                StaffUI.note(refreshNote, color: .cavnarAmber)
            }
            if items.isEmpty, pendingUndo == nil, offs != nil, shifts != nil {
                CavnarEmptyHearth(title: "No requests yet",
                                  message: "Can\u{2019}t make a shift? Open it on Today and choose Give up this shift or Swap this shift. Days away go in a time-off request.")
            }
        }
    }

    private func timeOffRow(_ t: StaffTimeOffRequest) -> some View {
        let key = "to\(t.id)"
        return VStack(alignment: .leading, spacing: 6) {
            HStack(alignment: .firstTextBaseline, spacing: 8) {
                HomeMixedText.make("Time off \(t.rangeLabel)", size: CavnarType.body, weight: 700, color: .cavnarInk)
                Spacer(minLength: 6)
                TonePill(text: t.chip.text, tone: t.chip.tone)
            }
            if let reason = t.reason, !reason.isEmpty {
                StaffUI.note("You said: \u{201C}\(reason)\u{201D}")
            }
            if let note = t.decisionNote, !note.isEmpty {
                StaffUI.note("Your manager: \u{201C}\(note)\u{201D}", color: .cavnarInk2)
            }
            if !t.stillScheduled.isEmpty {
                StaffUI.note("Still on your schedule: " + t.stillScheduled.map(\.label).joined(separator: "; ")
                             + ". Your manager moves these — you\u{2019}re on them until they do.",
                             color: .cavnarAmber)
            }
            HStack(spacing: 18) {
                if t.status == "pending" {
                    StaffTextButton(title: "Withdraw") {
                        startUndo(PendingUndo(key: key, label: "Withdrew time off \(t.rangeLabel)",
                                              path: "/staff/api/time-off/\(t.id)/withdraw"))
                    }
                } else if t.status == "approved" && t.canCancel {
                    StaffTextButton(title: "Call it off") {
                        startUndo(PendingUndo(key: key, label: "Called off time off \(t.rangeLabel)",
                                              path: "/staff/api/time-off/\(t.id)/cancel"))
                    }
                }
                if t.isLive {
                    StaffTextButton(title: "Message your manager") {
                        messaging = MessageSheet(context: .timeOff(t))
                    }
                }
            }
            if let e = rowError[key] { StaffUI.errorLine(e) }
        }
        .frame(maxWidth: .infinity, alignment: .leading)
        .cavnarCard()
        .accessibilityElement(children: .contain)
    }

    private func shiftRow(_ r: StaffMyShiftRequest) -> some View {
        let key = "sr\(r.id)"
        return VStack(alignment: .leading, spacing: 6) {
            HStack(alignment: .firstTextBaseline, spacing: 8) {
                VStack(alignment: .leading, spacing: 2) {
                    Text(r.tag.uppercased())
                        .font(.cavnarBody(CavnarType.kicker, weight: 700))
                        .tracking(1.0)
                        .foregroundStyle(Color.cavnarInk3)
                    HomeMixedText.make(r.shiftLabel, size: CavnarType.body, weight: 700, color: .cavnarInk)
                }
                Spacer(minLength: 6)
                TonePill(text: r.chip.text, tone: r.chip.tone)
            }
            if let target = r.targetLabel, let who = r.targetName {
                HomeMixedText.make("For \(who)\u{2019}s \(target)", size: CavnarType.secondary, color: .cavnarInk2)
            }
            if let detail = r.detail {
                StaffUI.note(detail)
            }
            if let note = r.decisionNote, !note.isEmpty {
                StaffUI.note("Your manager: \u{201C}\(note)\u{201D}", color: .cavnarInk2)
            }
            HStack(spacing: 18) {
                if r.canWithdraw {
                    StaffTextButton(title: "Withdraw") {
                        startUndo(PendingUndo(key: key, label: "Withdrew \(r.tag.lowercased()) \(r.shiftLabel)",
                                              path: "/staff/api/shift-requests/\(r.id)/withdraw"))
                    }
                }
                if r.isLive {
                    StaffTextButton(title: "Message your manager") {
                        messaging = MessageSheet(context: .shiftRequest(r))
                    }
                }
            }
            if let e = rowError[key] { StaffUI.errorLine(e) }
        }
        .frame(maxWidth: .infinity, alignment: .leading)
        .cavnarCard()
        .accessibilityElement(children: .contain)
    }

    // MARK: Actions

    private func perform(_ c: Confirm) async {
        switch c {
        case .ask(let a):
            await act("ask\(a.id)", "/staff/api/shift-requests/\(a.id)/respond", body: StaffAcceptBody(accept: true),
                      posted: a.managerApproved ? "Swapped" : "Accepted — waiting on your manager")
        case .offer(let o):
            await act("offer\(o.id)", "/staff/api/offers/\(o.id)/respond", body: StaffAcceptBody(accept: true),
                      posted: "It\u{2019}s on your schedule")
        case .claim(let s):
            await act("open\(s.id)", "/staff/api/open-shifts/\(s.id)/claim", body: StaffEmptyBody(),
                      posted: "It\u{2019}s on your schedule")
        }
    }

    /// One answer on one row: its error stays under that row; success is
    /// the posted check at the top of Waiting on you, never optimistic.
    private func act(_ key: String, _ path: String, body: any Encodable, posted: String) async {
        busy.insert(key)
        rowError[key] = nil
        defer { busy.remove(key) }
        do {
            let r: StaffOKResponse = try await staff.authed(path, method: .post, body: body)
            if r.ok {
                Haptic.success()
                postedLabel = posted
                await reload()
                await portal?.reloadBadges()
                await portal?.reloadShifts()
            } else {
                rowError[key] = r.error ?? "That didn\u{2019}t go through."
            }
        } catch {
            rowError[key] = StaffErrorText.message(error)
        }
    }

    /// Tier 1: the request leaves the list now and goes to the server only
    /// once the 7-second Undo window ends. A second withdraw sends the
    /// first at once.
    private func startUndo(_ p: PendingUndo) {
        if let earlier = pendingUndo, earlier != p {
            undoTask?.cancel()
            Task { await commit(earlier) }
        }
        rowError[p.key] = nil
        pendingUndo = p
        undoTask?.cancel()
        undoTask = Task { @MainActor in
            try? await Task.sleep(for: .seconds(7))
            guard !Task.isCancelled, pendingUndo == p else { return }
            await commit(p)
        }
    }

    private func cancelUndo() {
        undoTask?.cancel()
        undoTask = nil
        pendingUndo = nil
    }

    private func commit(_ p: PendingUndo) async {
        do {
            let r: StaffOKResponse = try await staff.authed(p.path, method: .post, body: StaffEmptyBody())
            if !r.ok { rowError[p.key] = r.error ?? "That didn\u{2019}t go through." }
        } catch {
            rowError[p.key] = StaffErrorText.message(error)
        }
        if pendingUndo == p { pendingUndo = nil }
        await reload()
        await portal?.reloadShifts()
    }

    // MARK: Loading

    func reload() async {
        async let b: StaffRequestsBoard = staff.authed("/staff/api/shift-requests")
        async let t: StaffTimeOffList = staff.authed("/staff/api/time-off")
        var failed = false
        do {
            board = .loaded(try await b)
        } catch {
            failed = true
            if board.value == nil { board = .failed(StaffErrorText.message(error, fallback: "Your shift requests didn\u{2019}t load.")) }
        }
        do {
            timeOff = .loaded(try await t.requests)
        } catch {
            failed = true
            if timeOff.value == nil { timeOff = .failed(StaffErrorText.message(error, fallback: "Try again.")) }
        }
        // A refresh that failed keeps what loaded before, and says so.
        refreshNote = failed && (board.value != nil || timeOff.value != nil)
            ? "Couldn\u{2019}t refresh just now — this is what loaded earlier." : nil
    }
}

// MARK: - Ask for time off

/// `StaffTimeOffSheet()` — the dates, an optional reason, one primary.
struct StaffTimeOffSheet: View {
    @Environment(StaffSessionStore.self) private var staff
    @Environment(\.dismiss) private var dismiss
    @State private var start = Calendar.current.date(byAdding: .day, value: 1, to: Date()) ?? Date()
    @State private var end = Calendar.current.date(byAdding: .day, value: 1, to: Date()) ?? Date()
    @State private var reason = ""
    @State private var sending = false
    @State private var error: String?
    @State private var posted: String?

    var body: some View {
        NavigationStack {
            ScrollView {
                VStack(alignment: .leading, spacing: 16) {
                    StaffUI.note("Your manager answers it in Cavnar AI. An approved range stays off the next schedule. Your usual week goes in My availability, on Me.")
                    DatePicker("First day off", selection: $start, in: Date()..., displayedComponents: .date)
                        .font(.cavnarBody(CavnarType.body))
                        .tint(Color.cavnarEmber)
                        .frame(minHeight: 44)
                    DatePicker("Last day off", selection: $end, in: start..., displayedComponents: .date)
                        .font(.cavnarBody(CavnarType.body))
                        .tint(Color.cavnarEmber)
                        .frame(minHeight: 44)
                    TextField("Why (optional)", text: $reason, axis: .vertical)
                        .lineLimit(1...4)
                        .cavnarTextFieldStyle()
                    Button {
                        Task { await send() }
                    } label: {
                        Group {
                            if sending { StaffBusyLabel(text: "Sending") } else { Text("Ask for these days") }
                        }
                        .frame(maxWidth: .infinity)
                    }
                    .buttonStyle(CavnarPrimaryButtonStyle(isDisabled: sending))
                    .disabled(sending)
                    if let error { StaffUI.errorLine(error) }
                }
                .padding(20)
            }
            .accountSheetChrome("Time off")
        }
        .onChange(of: start) { _, new in if end < new { end = new } }
        .cavnarPostedOverlay(posted) { dismiss() }
    }

    private func send() async {
        sending = true
        error = nil
        defer { sending = false }
        let body = StaffTimeOffBody(startDate: StaffDay.iso(start), endDate: StaffDay.iso(end),
                                    reason: reason.trimmingCharacters(in: .whitespacesAndNewlines))
        do {
            let r: StaffOKResponse = try await staff.authed("/staff/api/time-off", method: .post, body: body)
            if r.ok {
                Haptic.success()
                posted = "Sent to your manager"
            } else {
                error = r.error ?? "That didn\u{2019}t go through."
            }
        } catch {
            self.error = StaffErrorText.message(error)
        }
    }
}

// MARK: - Give up or swap a shift

/// "Give up this shift" (a drop) or "Swap this shift" (their shift for
/// yours: your manager's yes and theirs). Opened from a shift on Today, or
/// from a conflict after an availability save.
///
/// `StaffShiftChangeSheet(day: StaffWeekDay, mode:)` or, for one leg of a
/// double or a conflict, `StaffShiftChangeSheet(date:shiftStart:shiftEnd:mode:)`.
struct StaffShiftChangeSheet: View {
    enum Mode: Hashable { case drop, swap }

    let date: String
    let shiftStart: String
    let shiftEnd: String?
    let mode: Mode

    init(date: String, shiftStart: String, shiftEnd: String? = nil, mode: Mode) {
        self.date = date
        self.shiftStart = shiftStart
        self.shiftEnd = shiftEnd
        self.mode = mode
    }

    init(day: StaffWeekDay, mode: Mode) {
        self.init(date: day.date, shiftStart: day.shift?.shiftStart ?? "", shiftEnd: day.shift?.shiftEnd, mode: mode)
    }

    @Environment(StaffSessionStore.self) private var staff
    @Environment(\.dismiss) private var dismiss
    @State private var candidates: StaffLoad<StaffSwapCandidates> = .loading
    @State private var colleague: String?
    @State private var theirShiftId: String?
    @State private var pickingColleague = true
    @State private var pickingShift = false
    @State private var reason = ""
    @State private var sending = false
    @State private var error: String?
    @State private var posted: String?

    private var chosen: StaffSwapCandidate? { candidates.value?.colleagues.first { $0.name == colleague } }
    private var theirShift: StaffSwapCandidate.Shift? { chosen?.shifts.first { $0.id == theirShiftId } }
    private var ready: Bool { mode == .drop || theirShift != nil }

    var body: some View {
        NavigationStack {
            ScrollView {
                VStack(alignment: .leading, spacing: 16) {
                    HomeMixedText.make(StaffDay.shift(date, shiftStart, shiftEnd), size: CavnarType.emphasis,
                                       weight: 700, color: .cavnarInk)
                    if mode == .swap { swapPickers }
                    TextField(mode == .swap ? "Why (optional)" : "Why can\u{2019}t you work it? (optional)",
                              text: $reason, axis: .vertical)
                        .lineLimit(1...4)
                        .cavnarTextFieldStyle()
                    Button {
                        Task { await send() }
                    } label: {
                        Group {
                            if sending { StaffBusyLabel(text: "Sending") }
                            else { Text(mode == .swap ? "Ask to swap" : "Ask to give it up") }
                        }
                        .frame(maxWidth: .infinity)
                    }
                    .buttonStyle(CavnarPrimaryButtonStyle(isDisabled: sending || !ready))
                    .disabled(sending || !ready)
                    if let error { StaffUI.errorLine(error) }
                    StaffUI.note(mode == .swap
                                 ? "Both shifts move once your manager approves and your colleague says yes."
                                 : "Your manager decides. If they approve, you\u{2019}re still on it until someone on the team picks it up.")
                }
                .padding(20)
            }
            .accountSheetChrome(mode == .swap ? "Swap this shift" : "Give up this shift")
        }
        .task { if mode == .swap { await loadCandidates() } }
        .cavnarPostedOverlay(posted) { dismiss() }
    }

    @ViewBuilder
    private var swapPickers: some View {
        switch candidates {
        case .loading:
            StaffUI.loading("Finding who can swap")
        case .failed(let message):
            StaffUI.failedCard(message) { Task { await loadCandidates() } }
        case .loaded(let list):
            if list.colleagues.isEmpty {
                StaffUI.note(list.note ?? "Nobody on the app has a shift that week you could trade for.")
            } else {
                VStack(alignment: .leading, spacing: 10) {
                    CavnarDropdown(title: "Swap with", subtitle: colleague ?? "Choose a colleague",
                                   isExpanded: $pickingColleague) {
                        VStack(spacing: 0) {
                            ForEach(list.colleagues) { c in
                                choiceRow(c.name, selected: c.name == colleague) {
                                    colleague = c.name
                                    theirShiftId = nil          // never preselect their shift (WF-08)
                                    pickingColleague = false
                                    pickingShift = true
                                }
                            }
                        }
                    }
                    if let chosen {
                        CavnarDropdown(title: "For their shift", subtitle: theirShift?.label ?? "Choose their shift",
                                       isExpanded: $pickingShift) {
                            VStack(spacing: 0) {
                                ForEach(chosen.shifts) { s in
                                    choiceRow(s.label + (s.role.map { " · \($0)" } ?? ""), selected: s.id == theirShiftId,
                                              detail: s.checked ? nil : "Not checked yet — checked when you ask") {
                                        theirShiftId = s.id
                                        pickingShift = false
                                    }
                                }
                            }
                        }
                    }
                    if let note = list.note { StaffUI.note(note) }
                }
                .cavnarCard()
            }
        }
    }

    private func choiceRow(_ title: String, selected: Bool, detail: String? = nil,
                           action: @escaping () -> Void) -> some View {
        Button {
            Haptic.selection()
            action()
        } label: {
            HStack(spacing: 10) {
                VStack(alignment: .leading, spacing: 2) {
                    HomeMixedText.make(title, size: CavnarType.body, weight: selected ? 700 : 400, color: .cavnarInk)
                    if let detail {
                        Text(detail).font(.cavnarBody(CavnarType.caption)).foregroundStyle(Color.cavnarInk3)
                    }
                }
                Spacer(minLength: 8)
                if selected {
                    Image(systemName: "checkmark")
                        .font(.system(size: 13, weight: .bold))
                        .foregroundStyle(Color.cavnarEmber)
                }
            }
            .frame(minHeight: 44)
            .contentShape(Rectangle())
        }
        .buttonStyle(.plain)
        .accessibilityAddTraits(selected ? .isSelected : [])
    }

    private func loadCandidates() async {
        candidates = .loading
        do {
            candidates = .loaded(try await staff.staffGet("/staff/api/colleagues",
                                                          query: ["shift_date": date, "shift_start": shiftStart]))
        } catch {
            candidates = .failed(StaffErrorText.message(error, fallback: "Couldn\u{2019}t load who can swap."))
        }
    }

    private func send() async {
        sending = true
        error = nil
        defer { sending = false }
        let why = reason.trimmingCharacters(in: .whitespacesAndNewlines)
        var body = StaffShiftChangeBody(date: date, shiftStart: shiftStart, reason: why)
        if mode == .swap {
            guard let theirShift, let colleague else { return }
            body.kind = "swap"
            body.targetName = colleague
            body.targetDate = theirShift.date
            body.targetStart = theirShift.shiftStart
        }
        do {
            let r: StaffOKResponse = try await staff.authed("/staff/api/shift-requests", method: .post, body: body)
            if r.ok {
                Haptic.success()
                posted = mode == .swap ? "Asked — your manager and \(colleague ?? "your colleague") are told"
                                       : "Sent to your manager"
            } else {
                error = r.error ?? "That didn\u{2019}t go through."
            }
        } catch {
            self.error = StaffErrorText.message(error)
        }
    }
}
