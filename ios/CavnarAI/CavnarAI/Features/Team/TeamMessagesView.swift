import SwiftUI
import Observation

/// Team messages — direct messages between the restaurant's console logins
/// (parity audit 10/7/26 #71), the web's Messages panel beside the bell
/// (dashboard.html `team-msg-panel`). /mobile/api/team/* (mobile_api;
/// permissions.TEAM_MESSAGE — owners and managers). Every send pushes the
/// recipient's phone (models.send_team_message), and the push's typed Reply
/// answers from the lock screen (PushManager).
@Observable
@MainActor
final class TeamMessagesCenter {
    static let shared = TeamMessagesCenter()

    var showing = false
    /// The teammate whose thread opens with the inbox (a push or a link).
    var openWith: Int?
    private(set) var unread = 0
    /// False until the inbox answered for this login — a login without
    /// TEAM_MESSAGE (403) never sees the button.
    private(set) var available = false
    private var lastRefresh: Date?

    private let client: APIClient
    init(client: APIClient = .shared) { self.client = client }

    static let inboxPath = "/mobile/api/team/inbox"
    static let sendPath = "/mobile/api/team/messages"
    static func threadPath(_ userId: Int) -> String { "/mobile/api/team/messages/\(userId)" }

    /// Opens the inbox, on one teammate's thread when `userId` names one.
    func open(with userId: Int?) {
        openWith = (userId ?? 0) > 0 ? userId : nil
        showing = true
    }

    /// The unread count for the dot. Cheap, and at most every 30 seconds
    /// however many toolbars ask; `force` after a thread was read.
    func refresh(force: Bool = false) async {
        if !force, let lastRefresh, Date().timeIntervalSince(lastRefresh) < 30 { return }
        lastRefresh = Date()
        do {
            let r: TeamMessagesInboxResponse = try await client.send(Self.inboxPath, hapticOnError: false)
            available = r.ok
            unread = r.ok ? r.unreadTotal : 0
        } catch let e as APIClient.APIError where e.status == 403 || e.status == 404 {
            available = false
            unread = 0
        } catch {
            // Offline or a deploy: keep what was shown.
        }
    }

    /// A sign-out or a location switch: nothing carries over.
    func reset() {
        showing = false
        openWith = nil
        unread = 0
        available = false
        lastRefresh = nil
    }
}

/// The Messages button beside the bell, with a dot while anything is
/// unread. Hidden for a login that can't message (members).
struct CavnarMessagesButton: View {
    @State private var center = TeamMessagesCenter.shared

    var body: some View {
        Group {
            if center.available {
                Button {
                    Haptic.light()
                    center.open(with: nil)
                } label: {
                    Image(systemName: "bubble.left.and.bubble.right")
                        .font(.system(size: 15, weight: .semibold))
                        .foregroundStyle(Color.cavnarEmber2)
                        .frame(width: 34, height: 34)
                        .overlay(alignment: .topTrailing) {
                            if center.unread > 0 {
                                Circle()
                                    .fill(Color.cavnarEmber)
                                    .frame(width: 8, height: 8)
                                    .offset(x: 1, y: -1)
                            }
                        }
                        .cavnarToolbarIconGlass()
                }
                .buttonStyle(.plain)
                .tint(nil)
                .accessibilityLabel(center.unread > 0 ? "Messages, \(center.unread) unread" : "Messages")
            } else {
                // Something on screen, so the task below runs and the
                // button can appear once the inbox answers.
                Color.clear.frame(width: 1, height: 1).accessibilityHidden(true)
            }
        }
        .task { await center.refresh() }
    }
}

/// Presents the Messages sheet over whatever is on screen — RootView
/// carries it once, like the bell's inbox.
private struct TeamMessagesPresenter: ViewModifier {
    @State private var center = TeamMessagesCenter.shared

    func body(content: Content) -> some View {
        content
            .sheet(isPresented: Binding(get: { center.showing }, set: { center.showing = $0 }),
                   onDismiss: { Task { await center.refresh(force: true) } }) {
                TeamMessagesView(initialUserId: center.openWith)
                    .presentationDetents([.large])
            }
    }
}

extension View {
    func teamMessagesPresenter() -> some View { modifier(TeamMessagesPresenter()) }
}

private struct TeamDMRoute: Hashable {
    let userId: Int
    let name: String
}

/// Every teammate, most recent conversation first; a thread opens on tap.
struct TeamMessagesView: View {
    var initialUserId: Int? = nil
    @Environment(SessionStore.self) private var sessionStore

    @State private var teammates: [TeammateThread] = []
    @State private var loaded = false
    @State private var error: String?
    @State private var path: [TeamDMRoute] = []
    @State private var openedInitial = false

    var body: some View {
        NavigationStack(path: $path) {
            ScrollView {
                VStack(alignment: .leading, spacing: 16) {
                    if !loaded {
                        CavnarSkeletonLines(widths: [0.8, 0.6, 0.9, 0.5])
                    } else if let error, teammates.isEmpty {
                        Text(error).font(.cavnarBody(CavnarType.body)).foregroundStyle(Color.cavnarInk2)
                    } else if teammates.isEmpty {
                        // Manage team is the owner's: a manager is told who
                        // adds teammates, not pointed at a row they don't
                        // have (re-audit L18).
                        CavnarEmptyHearth(title: "Nobody to message yet",
                                          message: sessionStore.currentUser?.isOwner == true
                                            ? "Teammates you add in Account \u{2192} Manage team show up here."
                                            : "Ask the account owner to add teammates \u{2014} they show up here.")
                    } else {
                        VStack(spacing: 0) {
                            ForEach(Array(teammates.enumerated()), id: \.element.id) { i, t in
                                NavigationLink(value: TeamDMRoute(userId: t.userId, name: t.name)) {
                                    row(t, divider: i < teammates.count - 1)
                                }
                                .buttonStyle(.plain)
                                .simultaneousGesture(TapGesture().onEnded { Haptic.light() })
                            }
                        }
                        .accountCard()
                    }
                }
                .padding(20)
            }
            .cavnarEmberRefreshable { await load() }
            .accountSheetChrome("Messages")
            .navigationDestination(for: TeamDMRoute.self) { r in
                TeamDirectThreadView(userId: r.userId, name: r.name) { Task { await load() } }
            }
        }
        .task {
            await load()
            guard !openedInitial, let id = initialUserId else { return }
            openedInitial = true
            let name = teammates.first(where: { $0.userId == id })?.name ?? ""
            path = [TeamDMRoute(userId: id, name: name)]
        }
    }

    private func row(_ t: TeammateThread, divider: Bool) -> some View {
        VStack(spacing: 0) {
            HStack(spacing: 12) {
                Text(t.initials)
                    .font(.cavnarBody(CavnarType.caption, weight: 700))
                    .foregroundStyle(Color.cavnarEmber2)
                    .frame(width: 38, height: 38)
                    .background(Color.cavnarEmber.opacity(0.14), in: Circle())
                VStack(alignment: .leading, spacing: 3) {
                    HStack {
                        Text(t.name)
                            .font(.cavnarBody(CavnarType.body, weight: t.unread > 0 ? 700 : 600))
                            .foregroundStyle(Color.cavnarInk)
                            .lineLimit(1)
                        Spacer(minLength: 4)
                        if t.lastAt != nil {
                            HomeMixedText.make(TeamTime.when(t.lastAt), role: .caption)
                                .lineLimit(1)
                        }
                    }
                    HStack {
                        Text(t.lastMessage.map { (t.lastFromMe ? "You: " : "") + $0 } ?? (t.role.map { $0.capitalized } ?? ""))
                            .font(.cavnar(.secondary))
                            .foregroundStyle(t.unread > 0 ? Color.cavnarInk : Color.cavnarInk2)
                            .lineLimit(1)
                        Spacer(minLength: 4)
                        if t.unread > 0 {
                            Text(t.unread > 9 ? "9+" : "\(t.unread)")
                                .font(.cavnarNumber(CavnarType.caption, weight: 700))
                                .foregroundStyle(Color.white)
                                .padding(.horizontal, 7)
                                .frame(minHeight: 20)
                                .background(Capsule().fill(Color.cavnarEmberFill))
                                .accessibilityLabel("\(t.unread) unread")
                        }
                    }
                }
            }
            .padding(.vertical, 11)
            .contentShape(Rectangle())
            if divider { AccountRowDivider() }
        }
    }

    private func load() async {
        do {
            let r: TeamMessagesInboxResponse = try await APIClient.shared.send(TeamMessagesCenter.inboxPath,
                                                                               hapticOnError: false)
            loaded = true
            guard r.ok else { error = r.error ?? "Messages didn\u{2019}t load."; return }
            teammates = r.teammates
            error = nil
        } catch is CancellationError {
        } catch let e as APIClient.APIError {
            loaded = true
            error = e.message
        } catch {
            loaded = true
            self.error = "Messages didn\u{2019}t load."
        }
    }
}

/// One conversation: bubbles in runs, the time under each run's last one,
/// a composer pinned to the bottom. Opening it reads their messages.
struct TeamDirectThreadView: View {
    let userId: Int
    let name: String
    var onChange: () -> Void = {}

    @State private var messages: [TeamDirectMessage] = []
    @State private var myId: Int?
    @State private var loaded = false
    @State private var loadError: String?
    @State private var text = ""
    @State private var sending = false
    @State private var error: String?
    @FocusState private var composing: Bool

    private static let limit = 2000

    var body: some View {
        ScrollViewReader { proxy in
            ScrollView {
                VStack(alignment: .leading, spacing: 4) {
                    if !loaded {
                        CavnarSkeletonLines(widths: [0.6, 0.8, 0.5])
                    } else if let loadError, messages.isEmpty {
                        Text(loadError).font(.cavnarBody(CavnarType.body)).foregroundStyle(Color.cavnarInk2)
                    } else if messages.isEmpty {
                        CavnarEmptyHearth(title: "Say hello",
                                          message: "\(firstName) gets it on their phone.")
                    } else {
                        ForEach(Array(messages.enumerated()), id: \.element.id) { i, m in
                            bubble(m, endOfRun: Self.endsRun(messages, at: i, me: myId))
                        }
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
        .navigationTitle(name.isEmpty ? "Conversation" : name)
        .navigationBarTitleDisplayMode(.inline)
        .toolbar { cavnarTitleToolbar(name.isEmpty ? "Conversation" : name) }
        // A typed message isn't lost to Back or a swipe-down (re-audit M9).
        .cavnarDraftGuard(!text.trimmingCharacters(in: .whitespacesAndNewlines).isEmpty && !sending)
        .task {
            await reload()
            while !Task.isCancelled {
                try? await Task.sleep(nanoseconds: 15_000_000_000)
                if Task.isCancelled { break }
                await reload()
            }
        }
    }

    private var firstName: String { name.split(separator: " ").first.map(String.init) ?? "They" }

    /// The last bubble of one sender's run — where the time shows.
    static func endsRun(_ messages: [TeamDirectMessage], at i: Int, me: Int?) -> Bool {
        guard i + 1 < messages.count else { return true }
        return (messages[i + 1].senderId == me) != (messages[i].senderId == me)
    }

    private func bubble(_ m: TeamDirectMessage, endOfRun: Bool) -> some View {
        let mine = m.senderId == myId
        return VStack(alignment: mine ? .trailing : .leading, spacing: 3) {
            Text(m.body)
                .font(.cavnarBody(CavnarType.body))
                .foregroundStyle(Color.cavnarInk)
                .fixedSize(horizontal: false, vertical: true)
                .padding(.horizontal, 14)
                .padding(.vertical, 9)
                .background(mine ? Color.cavnarEmber.opacity(0.18) : Color.cavnarPaper2,
                            in: RoundedRectangle(cornerRadius: CavnarRadius.card))
            if endOfRun {
                HomeMixedText.make(TeamTime.when(m.createdAt), role: .caption)
                    .padding(.bottom, 8)
            }
        }
        .frame(maxWidth: .infinity, alignment: mine ? .trailing : .leading)
        .padding(mine ? .leading : .trailing, 40)
        .accessibilityElement(children: .combine)
        .accessibilityLabel("\(mine ? "You" : name): \(m.body)")
    }

    private var composer: some View {
        VStack(alignment: .leading, spacing: 6) {
            HStack(alignment: .bottom, spacing: 10) {
                TextField("Message \(firstName)", text: $text, axis: .vertical)
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
                .accessibilityLabel("Send")
            }
            if let error {
                Text(error).font(.cavnar(.secondary)).foregroundStyle(Color.cavnarRedText)
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
            let r: TeamDirectThreadResponse = try await APIClient.shared.send(TeamMessagesCenter.threadPath(userId),
                                                                              hapticOnError: false)
            let first = !loaded
            loaded = true
            guard r.ok else { loadError = r.error ?? "This conversation didn\u{2019}t load."; return }
            messages = r.messages
            myId = r.myId
            loadError = nil
            if first { onChange() }
        } catch is CancellationError {
        } catch let e as APIClient.APIError {
            loaded = true
            loadError = e.message
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
            let r: TeamDirectSendResponse = try await APIClient.shared.send(
                TeamMessagesCenter.sendPath, method: .post, body: TeamDirectSendBody(recipientId: userId, body: body),
                retryTransient: false)
            guard r.ok else { error = r.error ?? "Couldn\u{2019}t send it."; return }
            Haptic.success()
            text = ""
            if let m = r.message { messages.append(m) } else { await reload() }
            onChange()
        } catch let e as APIClient.APIError {
            error = e.message
        } catch {
            self.error = "Couldn\u{2019}t send it."
        }
    }
}
