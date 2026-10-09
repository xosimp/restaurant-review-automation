import SwiftUI
import Observation

/// The owner's Team inbox (parity audit 10/7/26 #10) — the web's Team inbox
/// in Labor's Studio (dashboard.html `lb2-inbox`), on the phone: what staff
/// say from the app (a message to the manager on duty, running late) and
/// the announcements sent to them, each with who has read it.
///
/// /mobile/api/labor/inbox* — the twins of the web's routes, one body each
/// (staff_comms_routes). Opening a thread reads it with the same call the
/// web makes (`manager_thread`, mark_read on), so the employee sees "Seen"
/// whichever screen the manager read it on.

@Observable
@MainActor
final class TeamInboxViewModel {
    enum State: Equatable {
        case loading
        case loaded
        /// The login can't decide staff requests (403), or the read failed.
        case unavailable(String)
    }

    private(set) var state: State = .loading
    private(set) var threads: [TeamInboxThread] = []
    private(set) var late: [TeamLateReport] = []
    private(set) var unread = 0
    private(set) var announcements: [TeamAnnouncement] = []
    private(set) var roles: [String] = []
    private(set) var announcementsError: String?
    var withdrawError: String?
    var posted: String?

    private let client: APIClient
    init(client: APIClient = .shared) { self.client = client }

    static let inboxPath = "/mobile/api/labor/inbox"
    static let announcementsPath = "/mobile/api/labor/inbox/announcements"
    static func threadPath(_ id: Int) -> String { "/mobile/api/labor/inbox/threads/\(id)" }
    static func replyPath(_ id: Int) -> String { "/mobile/api/labor/inbox/threads/\(id)/reply" }
    static func withdrawPath(_ id: Int) -> String { "/mobile/api/labor/inbox/announcements/\(id)/withdraw" }

    func load() async {
        async let i: TeamInboxResponse = client.send(Self.inboxPath, hapticOnError: false)
        async let a: TeamAnnouncementsResponse? = try? client.send(Self.announcementsPath, hapticOnError: false)
        do {
            let inbox = try await i
            if inbox.ok {
                threads = inbox.threads
                late = inbox.lateToday
                unread = inbox.unread
                state = .loaded
            } else {
                state = .unavailable(inbox.error ?? "The team inbox didn\u{2019}t load.")
            }
        } catch is CancellationError {
        } catch let error as APIClient.APIError {
            if case .loaded = state {} else { state = .unavailable(error.message) }
        } catch {
            if case .loaded = state {} else { state = .unavailable("The team inbox didn\u{2019}t load.") }
        }
        if let ann = await a, ann.ok {
            announcements = ann.announcements
            roles = ann.roles
            announcementsError = nil
        } else if announcements.isEmpty {
            announcementsError = "Announcements didn\u{2019}t load. Pull to try again."
        }
    }

    func reloadAnnouncements() async {
        if let ann: TeamAnnouncementsResponse = try? await client.send(Self.announcementsPath, hapticOnError: false),
           ann.ok {
            announcements = ann.announcements
            roles = ann.roles
        }
    }

    /// A thread was opened (and so read): its count leaves the badge.
    func markRead(_ threadId: Int) {
        guard let i = threads.firstIndex(where: { $0.threadId == threadId }), threads[i].unread > 0 else { return }
        unread = max(0, unread - threads[i].unread)
        Task { await load() }
    }

    func withdraw(_ a: TeamAnnouncement) async {
        withdrawError = nil
        do {
            let r: TeamWithdrawResponse = try await client.send(Self.withdrawPath(a.id), method: .post,
                                                                body: TeamEmptyBody(), retryTransient: false)
            if r.ok {
                Haptic.success()
                posted = "Withdrawn"
                await reloadAnnouncements()
            } else {
                withdrawError = r.error ?? "Couldn\u{2019}t withdraw it."
            }
        } catch let error as APIClient.APIError {
            withdrawError = error.message
        } catch {
            withdrawError = "Couldn\u{2019}t withdraw it."
        }
    }
}

/// What opens the Team inbox sheet — the list, or one thread over it.
struct TeamInboxTarget: Identifiable, Hashable {
    let threadId: Int?
    var id: String { "team-inbox-\(threadId ?? 0)" }
}

/// Where the inbox's navigation stack can go.
enum TeamInboxRoute: Hashable {
    case thread(id: Int, name: String)
    case announcement(Int)
}

struct TeamInboxView: View {
    /// A `labor/inbox?thread=N` link's thread, opened over the list once.
    var initialThreadId: Int? = nil

    @State private var viewModel = TeamInboxViewModel()
    @State private var path: [TeamInboxRoute] = []
    @State private var openedInitial = false
    @State private var composing = false
    @State private var confirmWithdraw: TeamAnnouncement?

    init(initialThreadId: Int? = nil) {
        self.initialThreadId = initialThreadId
    }

    var body: some View {
        NavigationStack(path: $path) {
            ScrollView {
                VStack(alignment: .leading, spacing: 22) {
                    header
                    switch viewModel.state {
                    case .loading:
                        CavnarSkeletonLines(widths: [0.7, 1.0, 0.85, 0.6])
                    case .unavailable(let why):
                        Text(why)
                            .font(.cavnarBody(CavnarType.body))
                            .foregroundStyle(Color.cavnarInk3)
                            .fixedSize(horizontal: false, vertical: true)
                    case .loaded:
                        if !viewModel.late.isEmpty { lateStrip }
                        messages
                        announcements
                    }
                }
                .padding(20)
            }
            .cavnarEmberRefreshable { await viewModel.load() }
            .accountSheetChrome("Team inbox")
            .navigationDestination(for: TeamInboxRoute.self) { route in
                switch route {
                case .thread(let id, let name):
                    TeamThreadView(threadId: id, name: name) { viewModel.markRead(id) }
                case .announcement(let id):
                    if let a = viewModel.announcements.first(where: { $0.id == id }) {
                        TeamAnnouncementDetail(announcement: a) { confirmWithdraw = a }
                    }
                }
            }
        }
        .task {
            await viewModel.load()
            openInitialThread()
        }
        .sheet(isPresented: $composing) {
            TeamAnnouncementComposer(roles: viewModel.roles) { line in
                viewModel.posted = line
                Task { await viewModel.reloadAnnouncements() }
            }
            .presentationDetents([.large])
        }
        .confirmationDialog(confirmWithdraw.map { "Withdraw \u{201C}\($0.title)\u{201D}?" } ?? "",
                            isPresented: Binding(get: { confirmWithdraw != nil },
                                                 set: { if !$0 { confirmWithdraw = nil } }),
                            titleVisibility: .visible) {
            Button("Withdraw it", role: .destructive) {
                guard let a = confirmWithdraw else { return }
                confirmWithdraw = nil
                if path.last == .announcement(a.id) { path.removeLast() }
                Task { await viewModel.withdraw(a) }
            }
            Button("Keep it", role: .cancel) { confirmWithdraw = nil }
        } message: {
            Text("It leaves every inbox. Who read it stays on record.")
        }
        .cavnarPostedOverlay(viewModel.posted) { viewModel.posted = nil }
    }

    /// The thread a push or link named, once the list has loaded.
    private func openInitialThread() {
        guard !openedInitial, let id = initialThreadId, id > 0 else { return }
        openedInitial = true
        let name = viewModel.threads.first(where: { $0.threadId == id })?.employeeName ?? ""
        path = [.thread(id: id, name: name)]
    }

    private var header: some View {
        VStack(alignment: .leading, spacing: 8) {
            HStack(alignment: .lastTextBaseline) {
                VStack(alignment: .leading, spacing: CavnarSpace.xxs) {
                    CavnarKicker("Team inbox")
                    Text("Your team").cavnarText(.headline)
                }
                Spacer(minLength: CavnarSpace.s)
                if viewModel.unread > 0 {
                    HomeMixedText.make("\(viewModel.unread) unread", role: .secondary, color: .cavnarEmber2)
                }
            }
            // How the inbox works — only while there's nothing in it yet
            // (iOS readability round: it sat above every conversation).
            if viewModel.threads.isEmpty && viewModel.announcements.isEmpty {
                Text("Staff reach the manager on duty from the app; you answer here and it goes to their phone. "
                     + "An announcement goes to each person\u{2019}s phone, and they tap Got it.")
                    .cavnarText(.secondary)
                    .fixedSize(horizontal: false, vertical: true)
            }
        }
    }

    // MARK: Running late today

    private var lateStrip: some View {
        VStack(alignment: .leading, spacing: 8) {
            CavnarKicker("Running late today")
            ForEach(viewModel.late) { r in
                HStack(alignment: .top, spacing: 12) {
                    Image(systemName: "clock.badge.exclamationmark")
                        .font(.system(size: 16, weight: .semibold))
                        .foregroundStyle(Color.cavnarAmber)
                        .frame(width: 24)
                    VStack(alignment: .leading, spacing: 3) {
                        HomeMixedText.make(r.headline, size: CavnarType.body, weight: 700)
                            .fixedSize(horizontal: false, vertical: true)
                        if !r.detail.isEmpty {
                            HomeMixedText.make(r.detail, size: CavnarType.secondary, color: .cavnarInk3)
                                .fixedSize(horizontal: false, vertical: true)
                        }
                    }
                    Spacer(minLength: 0)
                }
                .padding(14)
                .background(Color.cavnarAmber.opacity(0.10), in: RoundedRectangle(cornerRadius: CavnarRadius.card))
                .overlay(RoundedRectangle(cornerRadius: CavnarRadius.card)
                    .strokeBorder(Color.cavnarAmber.opacity(0.28), lineWidth: 1))
                .accessibilityElement(children: .combine)
            }
        }
    }

    // MARK: Messages

    private var messages: some View {
        VStack(alignment: .leading, spacing: 8) {
            CavnarKicker("Messages")
            if viewModel.threads.isEmpty {
                Text("No messages yet. Staff write to the manager on duty from the app.")
                    .font(.cavnarBody(CavnarType.body))
                    .foregroundStyle(Color.cavnarInk3)
                    .fixedSize(horizontal: false, vertical: true)
                    .padding(.vertical, 6)
            } else {
                VStack(spacing: 0) {
                    ForEach(Array(viewModel.threads.enumerated()), id: \.element.id) { i, t in
                        NavigationLink(value: TeamInboxRoute.thread(id: t.threadId, name: t.employeeName)) {
                            threadRow(t, divider: i < viewModel.threads.count - 1)
                        }
                        .buttonStyle(.plain)
                        .simultaneousGesture(TapGesture().onEnded { Haptic.light() })
                    }
                }
                .accountCard()
            }
        }
    }

    private func threadRow(_ t: TeamInboxThread, divider: Bool) -> some View {
        VStack(spacing: 0) {
            HStack(alignment: .center, spacing: 12) {
                VStack(alignment: .leading, spacing: 4) {
                    HStack(spacing: 8) {
                        Text(t.employeeName)
                            .font(.cavnarBody(CavnarType.body, weight: t.unread > 0 ? 700 : 600))
                            .foregroundStyle(Color.cavnarInk)
                            .lineLimit(1)
                        if t.unread > 0 {
                            Text("\(t.unread)")
                                .font(.cavnarNumber(CavnarType.caption, weight: 700))
                                .foregroundStyle(Color.white)
                                .padding(.horizontal, 7)
                                .frame(minHeight: 20)
                                .background(Capsule().fill(Color.cavnarEmber))
                                .accessibilityLabel("\(t.unread) unread")
                        }
                        Spacer(minLength: 4)
                        HomeMixedText.make(TeamTime.when(t.lastAt), role: .caption)
                            .lineLimit(1)
                    }
                    Text(t.preview)
                        .font(.cavnar(.secondary))
                        .foregroundStyle(t.unread > 0 ? Color.cavnarInk : Color.cavnarInk2)
                        .lineLimit(2)
                }
                Image(systemName: "chevron.right")
                    .font(.system(size: 12, weight: .bold))
                    .foregroundStyle(Color.cavnarInk3)
            }
            .padding(.vertical, 12)
            .contentShape(Rectangle())
            if divider { AccountRowDivider() }
        }
    }

    // MARK: Announcements

    private var announcements: some View {
        VStack(alignment: .leading, spacing: 10) {
            HStack {
                CavnarKicker("Announcements")
                Spacer()
                Button {
                    Haptic.light()
                    composing = true
                } label: {
                    Label("New", systemImage: "plus")
                        .font(.cavnarBody(CavnarType.secondary, weight: 700))
                        .foregroundStyle(Color.cavnarEmber2)
                        .frame(minHeight: 44)
                        .contentShape(Rectangle())
                }
                .buttonStyle(.plain)
                .accessibilityLabel("New announcement")
            }
            if let error = viewModel.withdrawError {
                Text(error).font(.cavnar(.secondary)).foregroundStyle(Color.cavnarRedText)
            }
            if viewModel.announcements.isEmpty {
                if let error = viewModel.announcementsError {
                    Text(error).font(.cavnarBody(CavnarType.body)).foregroundStyle(Color.cavnarInk3)
                } else {
                    Text("Nothing sent yet. An announcement reaches each person\u{2019}s phone, and you see who has read it.")
                        .font(.cavnarBody(CavnarType.body))
                        .foregroundStyle(Color.cavnarInk3)
                        .fixedSize(horizontal: false, vertical: true)
                }
                Button {
                    Haptic.light()
                    composing = true
                } label: {
                    Text("Write an announcement").frame(maxWidth: .infinity)
                }
                .buttonStyle(CavnarSecondaryButtonStyle())
            } else {
                ForEach(viewModel.announcements) { a in
                    NavigationLink(value: TeamInboxRoute.announcement(a.id)) {
                        TeamAnnouncementRow(announcement: a)
                    }
                    .buttonStyle(.plain)
                    .contextMenu {
                        if a.canWithdraw {
                            Button(role: .destructive) { confirmWithdraw = a } label: {
                                Label("Withdraw", systemImage: "arrow.uturn.backward")
                            }
                        }
                    }
                }
            }
        }
    }
}

/// One announcement in the list: title (Urgent first), the start of the
/// body, then who it went to and "N of M read".
struct TeamAnnouncementRow: View {
    let announcement: TeamAnnouncement

    var body: some View {
        let a = announcement
        VStack(alignment: .leading, spacing: 6) {
            HStack(alignment: .firstTextBaseline, spacing: 6) {
                if a.isUrgent {
                    Text("Urgent")
                        .font(.cavnarBody(CavnarType.caption, weight: 700))
                        .foregroundStyle(Color.cavnarRedText)
                }
                Text(a.title)
                    .font(.cavnarBody(CavnarType.body, weight: 700))
                    .foregroundStyle(a.withdrawn ? Color.cavnarInk3 : Color.cavnarInk)
                    .lineLimit(2)
                Spacer(minLength: 4)
                Image(systemName: "chevron.right")
                    .font(.system(size: 12, weight: .bold))
                    .foregroundStyle(Color.cavnarInk3)
            }
            if !a.body.isEmpty {
                Text(a.body)
                    .font(.cavnarBody(CavnarType.secondary))
                    .foregroundStyle(Color.cavnarInk2)
                    .lineLimit(3)
            }
            HomeMixedText.make(Self.metaLine(a), size: CavnarType.caption, color: .cavnarInk3)
                .fixedSize(horizontal: false, vertical: true)
            if a.recipients > 0 && !a.withdrawn {
                GeometryReader { geo in
                    ZStack(alignment: .leading) {
                        Capsule().fill(Color.cavnarPaper3.opacity(0.6))
                        Capsule()
                            .fill(LinearGradient(colors: [Color.cavnarEmber2, Color.cavnarEmber],
                                                 startPoint: .leading, endPoint: .trailing))
                            .frame(width: geo.size.width * CGFloat(a.read) / CGFloat(max(a.recipients, 1)))
                    }
                }
                .frame(height: 4)
                .accessibilityHidden(true)
            }
        }
        .padding(14)
        .background(Color.cavnarPaper2.opacity(0.6), in: RoundedRectangle(cornerRadius: CavnarRadius.card))
        .overlay(RoundedRectangle(cornerRadius: CavnarRadius.card)
            .strokeBorder(Color.cavnarPaper3.opacity(0.5), lineWidth: 1))
        .contentShape(Rectangle())
        .accessibilityElement(children: .combine)
        .accessibilityHint("Opens who has read it")
    }

    /// "Everyone · 10/3/26 · by Erik · 3 of 7 read · until 10/9/26 · withdrawn".
    static func metaLine(_ a: TeamAnnouncement) -> String {
        var bits = [a.audienceLabel]
        if let created = a.createdAt, !created.isEmpty { bits.append(CavnarDate.mdyLocal(created)) }
        if !a.createdByName.isEmpty { bits.append("by \(a.createdByName)") }
        bits.append(a.readLine)
        if let exp = a.expiresOn, !exp.isEmpty { bits.append((a.expired ? "ended " : "until ") + CavnarDate.mdy(exp)) }
        if a.withdrawn { bits.append("withdrawn") }
        return bits.joined(separator: " \u{00B7} ")
    }
}

/// Who read an announcement and who hasn't yet, with Withdraw.
struct TeamAnnouncementDetail: View {
    let announcement: TeamAnnouncement
    let onWithdraw: () -> Void

    var body: some View {
        let a = announcement
        ScrollView {
            VStack(alignment: .leading, spacing: 20) {
                VStack(alignment: .leading, spacing: 8) {
                    if a.isUrgent {
                        AccountChip(text: "Urgent", tint: .cavnarRed)
                    }
                    Text(a.title)
                        .font(.cavnarHeadline(CavnarText.title.size))
                        .foregroundStyle(Color.cavnarInk)
                        .fixedSize(horizontal: false, vertical: true)
                    if !a.body.isEmpty {
                        Text(a.body)
                            .font(.cavnarBody(CavnarType.body))
                            .foregroundStyle(Color.cavnarInk2)
                            .fixedSize(horizontal: false, vertical: true)
                    }
                    HomeMixedText.make(TeamAnnouncementRow.metaLine(a), size: CavnarType.caption, color: .cavnarInk3)
                        .fixedSize(horizontal: false, vertical: true)
                }
                HStack(spacing: 10) {
                    AccountStatTile(label: "Read", value: "\(a.read)", valueIsNumber: true)
                    AccountStatTile(label: "Not yet", value: "\(max(0, a.recipients - a.read))", valueIsNumber: true)
                }
                if !a.unreadNames.isEmpty && !a.withdrawn {
                    AccountSection(kicker: "Not read yet") {
                        ForEach(Array(a.unreadNames.enumerated()), id: \.offset) { i, name in
                            AccountKVRow(label: name, showsDivider: i < a.unreadNames.count - 1) { EmptyView() }
                        }
                    }
                }
                if !a.readBy.isEmpty {
                    AccountSection(kicker: "Read") {
                        ForEach(Array(a.readBy.enumerated()), id: \.offset) { i, r in
                            AccountKVRow(label: r.name, showsDivider: i < a.readBy.count - 1) {
                                Text(TeamTime.when(r.ackedAt))
                                    .font(.cavnarNumber(CavnarType.secondary, weight: 500))
                                    .foregroundStyle(Color.cavnarInk3)
                            }
                        }
                    }
                }
                if a.canWithdraw {
                    Button(role: .destructive) {
                        Haptic.light()
                        onWithdraw()
                    } label: {
                        Text("Withdraw it").frame(maxWidth: .infinity)
                    }
                    .buttonStyle(CavnarSecondaryButtonStyle())
                }
            }
            .padding(20)
        }
        .cavnarModuleBackground()
        .navigationTitle("Announcement")
        .navigationBarTitleDisplayMode(.inline)
        .toolbar { cavnarTitleToolbar("Announcement") }
    }
}

// MARK: - Thread

/// One employee's thread: bubbles oldest first, a composer pinned to the
/// bottom, "Seen" under the last of the managers' messages the employee
/// has read. Opening it reads it (GET marks it read, the web's own call).
struct TeamThreadView: View {
    let threadId: Int
    let name: String
    var onRead: () -> Void = {}

    @State private var messages: [TeamThreadMessage] = []
    @State private var employeeName = ""
    @State private var loaded = false
    @State private var loadError: String?
    @State private var text = ""
    @State private var sending = false
    @State private var error: String?
    @State private var note: String?
    @FocusState private var composing: Bool

    private static let limit = 1000

    var body: some View {
        ScrollViewReader { proxy in
            ScrollView {
                VStack(alignment: .leading, spacing: 12) {
                    if !loaded {
                        CavnarSkeletonLines(widths: [0.6, 0.8, 0.5])
                    } else if let loadError, messages.isEmpty {
                        Text(loadError).font(.cavnarBody(CavnarType.body)).foregroundStyle(Color.cavnarInk3)
                    } else if messages.isEmpty {
                        CavnarEmptyHearth(title: "No messages yet",
                                          message: "Write to \(displayName) here; it goes to their phone.")
                    } else {
                        ForEach(messages) { bubble($0, seen: $0.id == lastSeenId) }
                    }
                    Color.clear.frame(height: 1).id("bottom")
                }
                .padding(20)
            }
            .onChange(of: messages.count) { _, _ in
                withAnimation(.easeOut(duration: 0.25)) { proxy.scrollTo("bottom", anchor: .bottom) }
            }
            .onAppear { proxy.scrollTo("bottom", anchor: .bottom) }
        }
        .safeAreaInset(edge: .bottom) { composer }
        .cavnarModuleBackground()
        .navigationTitle(displayName)
        .navigationBarTitleDisplayMode(.inline)
        .toolbar { cavnarTitleToolbar(displayName) }
        .task {
            await reload()
            // New messages while it's open: read again every 20 seconds.
            while !Task.isCancelled {
                try? await Task.sleep(nanoseconds: 20_000_000_000)
                if Task.isCancelled { break }
                await reload()
            }
        }
    }

    private var displayName: String { employeeName.isEmpty ? (name.isEmpty ? "Conversation" : name) : employeeName }

    /// The newest manager message the employee has read — where "Seen" sits.
    private var lastSeenId: Int? {
        Self.lastSeenId(messages)
    }

    static func lastSeenId(_ messages: [TeamThreadMessage]) -> Int? {
        messages.last(where: { $0.isManager && !($0.readAt ?? "").isEmpty })?.id
    }

    private func bubble(_ m: TeamThreadMessage, seen: Bool) -> some View {
        let mine = m.isManager
        let who = mine ? (m.senderName.isEmpty ? "A manager" : m.senderName) : (m.senderName.isEmpty ? displayName : m.senderName)
        return VStack(alignment: mine ? .trailing : .leading, spacing: 4) {
            if let day = m.shiftDate, !day.isEmpty {
                HomeMixedText.make("About " + CavnarDate.mdy(day), size: CavnarType.caption, color: .cavnarInk3)
            }
            Text(m.body)
                .font(.cavnarBody(CavnarType.body))
                .foregroundStyle(Color.cavnarInk)
                .fixedSize(horizontal: false, vertical: true)
                .padding(.horizontal, 14)
                .padding(.vertical, 10)
                .background(mine ? Color.cavnarEmber.opacity(0.16) : Color.cavnarPaper2,
                            in: RoundedRectangle(cornerRadius: CavnarRadius.card))
                .overlay(RoundedRectangle(cornerRadius: CavnarRadius.card)
                    .strokeBorder(Color.cavnarPaper3.opacity(mine ? 0 : 0.6), lineWidth: 1))
            HomeMixedText.make([who, TeamTime.when(m.createdAt)].filter { !$0.isEmpty }.joined(separator: " \u{00B7} "),
                               size: CavnarType.caption, color: .cavnarInk3)
            if seen {
                Text("Seen").font(.cavnarBody(CavnarType.caption, weight: 600)).foregroundStyle(Color.cavnarInk3)
            }
        }
        .frame(maxWidth: .infinity, alignment: mine ? .trailing : .leading)
        .padding(mine ? .leading : .trailing, 36)
        .id(m.id)
        .accessibilityElement(children: .combine)
    }

    private var composer: some View {
        VStack(alignment: .leading, spacing: 8) {
            if let note {
                Text(note).font(.cavnarBody(CavnarType.caption)).foregroundStyle(Color.cavnarInk3)
            }
            HStack(alignment: .bottom, spacing: 10) {
                TextField("Reply to \(displayName.split(separator: " ").first.map(String.init) ?? displayName)",
                          text: $text, axis: .vertical)
                    .lineLimit(1...5)
                    .focused($composing)
                    .cavnarTextFieldStyle()
                    .onChange(of: text) { _, v in if v.count > Self.limit { text = String(v.prefix(Self.limit)) } }
                Button {
                    Task { await send() }
                } label: {
                    Group {
                        if sending {
                            CavnarShimmerLine(color: .white).frame(width: 22)
                        } else {
                            Image(systemName: "arrow.up").font(.system(size: 17, weight: .bold))
                        }
                    }
                    .foregroundStyle(Color.white)
                    .frame(width: 44, height: 44)
                    .background(Circle().fill(trimmed.isEmpty ? Color.cavnarPaper3 : Color.cavnarEmber))
                }
                .buttonStyle(.plain)
                .disabled(sending || trimmed.isEmpty)
                .accessibilityLabel("Send reply")
            }
            if let error {
                Text(error).font(.cavnar(.secondary)).foregroundStyle(Color.cavnarRedText)
                    .fixedSize(horizontal: false, vertical: true)
            }
        }
        .padding(.horizontal, 20)
        .padding(.top, 10)
        .padding(.bottom, 12)
        .background(Color.cavnarPaper.opacity(0.97))
    }

    private var trimmed: String { text.trimmingCharacters(in: .whitespacesAndNewlines) }

    private func reload() async {
        do {
            let r: TeamThreadResponse = try await APIClient.shared.send(TeamInboxViewModel.threadPath(threadId),
                                                                        hapticOnError: false)
            loaded = true
            guard r.ok else { loadError = r.error ?? "This conversation didn\u{2019}t load."; return }
            let hadUnread = messages.isEmpty
            messages = r.messages
            if let n = r.thread?.employeeName, !n.isEmpty { employeeName = n }
            loadError = nil
            if hadUnread { onRead() }
        } catch is CancellationError {
        } catch let error as APIClient.APIError {
            loaded = true
            loadError = error.message
        } catch {
            loaded = true
            loadError = "This conversation didn\u{2019}t load."
        }
    }

    private func send() async {
        let body = trimmed
        guard !body.isEmpty else { return }
        sending = true
        error = nil
        defer { sending = false }
        do {
            let r: TeamReplyResponse = try await APIClient.shared.send(
                TeamInboxViewModel.replyPath(threadId), method: .post, body: TeamReplyBody(body: body),
                retryTransient: false)
            guard r.ok else { error = r.error ?? "Couldn\u{2019}t send it."; return }
            Haptic.success()
            text = ""
            composing = false
            if let m = r.message { messages.append(m) } else { await reload() }
            note = r.deliveredVia == nil ? "Saved \u{2014} they see it next time they open the app." : nil
        } catch let e as APIClient.APIError {
            error = e.message
        } catch {
            self.error = "Couldn\u{2019}t send it."
        }
    }
}

// MARK: - Announcement composer

/// A post to staff: who it goes to (everyone, one role, one day's
/// schedule), urgent or not, an optional last day — native pickers only —
/// and a confirm before it goes to their phones.
struct TeamAnnouncementComposer: View {
    let roles: [String]
    var onSent: (String?) -> Void

    @Environment(\.dismiss) private var dismiss
    @State private var title = ""
    @State private var message = ""
    @State private var audience: TeamAnnounceBody.Audience = .all
    @State private var role = ""
    @State private var day = ""
    @State private var urgent = false
    @State private var expires = ""
    @State private var confirming = false
    @State private var sending = false
    @State private var error: String?

    private static var todayISO: String { CavnarDate.isoDay(Date()) }

    var body: some View {
        NavigationStack {
            ScrollView {
                VStack(alignment: .leading, spacing: 20) {
                    AccountSection(kicker: "What they need to know") {
                        VStack(alignment: .leading, spacing: 12) {
                            TextField("Title (Closing at 9 tonight)", text: $title)
                                .cavnarTextFieldStyle()
                                .onChange(of: title) { _, v in if v.count > 120 { title = String(v.prefix(120)) } }
                            TextField("Message", text: $message, axis: .vertical)
                                .lineLimit(3...8)
                                .cavnarTextFieldStyle()
                                .onChange(of: message) { _, v in if v.count > 2000 { message = String(v.prefix(2000)) } }
                            Text("The title is what the lock screen shows.")
                                .font(.cavnarBody(CavnarType.caption))
                                .foregroundStyle(Color.cavnarInk3)
                        }
                        .padding(.vertical, 8)
                    }

                    AccountSection(kicker: "Who it goes to") {
                        VStack(alignment: .leading, spacing: 12) {
                            CavnarSegmentedControl(selection: $audience, options: TeamAnnounceBody.Audience.allCases) {
                                $0.label
                            }
                            switch audience {
                            case .all:
                                EmptyView()
                            case .role:
                                if roles.isEmpty {
                                    Text("No roles yet \u{2014} they come from the roster.")
                                        .font(.cavnarBody(CavnarType.secondary))
                                        .foregroundStyle(Color.cavnarInk3)
                                } else {
                                    AccountKVRow(label: "Role", showsDivider: false) {
                                        Picker("Role", selection: $role) {
                                            ForEach(roles, id: \.self) { Text($0).tag($0) }
                                        }
                                        .pickerStyle(.menu)
                                        .tint(Color.cavnarEmber2)
                                    }
                                }
                            case .shiftDate:
                                AccountKVRow(label: "Day", showsDivider: false) {
                                    CavnarDateChip(iso: $day, earliest: Date(), accessibilityName: "Day")
                                }
                            }
                        }
                        .padding(.vertical, 8)
                    }

                    AccountSection(kicker: "How") {
                        AccountSwitchRow(label: "Urgent",
                                         detail: "Breaks through Focus on their phones. For a change to tonight, not news.",
                                         isOn: $urgent)
                        AccountKVRow(label: "Last day", showsDivider: false) {
                            HStack(spacing: 8) {
                                CavnarDateChip(iso: $expires, earliest: Date(), accessibilityName: "Last day")
                                if !expires.isEmpty {
                                    Button {
                                        Haptic.light()
                                        expires = ""
                                    } label: {
                                        Image(systemName: "xmark")
                                            .font(.system(size: 12, weight: .bold))
                                            .foregroundStyle(Color.cavnarInk3)
                                            .frame(width: 44, height: 44)
                                            .contentShape(Rectangle())
                                    }
                                    .buttonStyle(.plain)
                                    .accessibilityLabel("No last day")
                                }
                            }
                        }
                    }
                    if audience == .shiftDate {
                        Text("A note for one day\u{2019}s crew ends with that day.")
                            .font(.cavnarBody(CavnarType.caption))
                            .foregroundStyle(Color.cavnarInk3)
                    }

                    if let error {
                        Text(error).font(.cavnar(.secondary)).foregroundStyle(Color.cavnarRedText)
                            .fixedSize(horizontal: false, vertical: true)
                    }

                    Button {
                        Haptic.light()
                        if let problem = validation { error = problem } else { error = nil; confirming = true }
                    } label: {
                        Group {
                            if sending { CavnarShimmerText(text: "Sending\u{2026}") } else { Text("Send to staff") }
                        }
                        .frame(maxWidth: .infinity)
                    }
                    .buttonStyle(CavnarPrimaryButtonStyle(isDisabled: sending))
                    .disabled(sending)
                }
                .padding(20)
            }
            .accountSheetChrome("New announcement")
        }
        .onAppear { if role.isEmpty { role = roles.first ?? "" } }
        .confirmationDialog("Send \u{201C}\(title.trimmingCharacters(in: .whitespaces))\u{201D} to \(audienceWords)?",
                            isPresented: $confirming, titleVisibility: .visible) {
            Button(urgent ? "Send it, urgent" : "Send it") { Task { await send() } }
            Button("Not yet", role: .cancel) {}
        } message: {
            Text("It goes to each person\u{2019}s phone now.")
        }
    }

    /// The words the confirm names the audience with.
    private var audienceWords: String {
        switch audience {
        case .all: return "everyone"
        case .role: return role.isEmpty ? "one role" : role
        case .shiftDate: return day.isEmpty ? "one day\u{2019}s schedule" : "the \(CavnarDate.mdy(day)) schedule"
        }
    }

    /// What the server would refuse, said before the confirm.
    private var validation: String? {
        if title.trimmingCharacters(in: .whitespacesAndNewlines).isEmpty { return "Give it a title." }
        if audience == .role && role.isEmpty { return "Pick the role it goes to." }
        if audience == .shiftDate && day.isEmpty { return "Pick the day." }
        return nil
    }

    private func send() async {
        sending = true
        error = nil
        defer { sending = false }
        let body = TeamAnnounceBody.make(title: title, body: message, urgent: urgent, audience: audience,
                                         role: role, day: day, expires: expires)
        do {
            let r: TeamAnnounceResponse = try await APIClient.shared.send(
                TeamInboxViewModel.announcementsPath, method: .post, body: body, retryTransient: false)
            guard r.ok else { error = r.error ?? "Couldn\u{2019}t send it."; return }
            Haptic.success()
            onSent(r.sentLine)
            dismiss()
        } catch let e as APIClient.APIError {
            error = e.message
        } catch {
            self.error = "Couldn\u{2019}t send it."
        }
    }
}
