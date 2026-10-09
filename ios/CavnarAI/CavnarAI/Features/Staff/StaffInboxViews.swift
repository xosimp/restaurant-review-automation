import SwiftUI

// The employee's inbox (employee audit wave 2, I3 — H8, H15, V6, B5/B6):
// announcements with "Got it", the one thread with the managers, and the
// post-shift pulse. Payloads are in StaffRequestModels.swift.

// MARK: - Inbox

/// `StaffInboxView(store: StaffPortalStore)` — a full sheet (the container
/// opens it from Today's tray button): the thread with the managers first,
/// then announcements, each with a right-aligned Got it. Calls go through the environment's
/// StaffSessionStore; `store.reloadBadges()` runs after an ack.
struct StaffInboxView: View {
    let store: StaffPortalStore

    init(store: StaffPortalStore) { self.store = store }

    @Environment(StaffSessionStore.self) private var staff
    @Environment(\.scenePhase) private var scenePhase
    @State private var inbox: StaffLoad<StaffInboxPayload> = .loading
    @State private var acking: Set<Int> = []
    @State private var ackError: [Int: String] = [:]
    @State private var messaging = false
    /// Translated announcements the reader flipped to what the manager wrote.
    @State private var showingOriginal: Set<Int> = []
    /// The announcement a push opened (store.focus), ringed until read.
    @State private var focusedAnnouncement: Int?

    var body: some View {
        NavigationStack {
            ScrollViewReader { proxy in
            ScrollView {
                VStack(alignment: .leading, spacing: 12) {
                    StaffPulseCard(store: store)
                    // The managers' replies first: a conversation waits on
                    // this person; an announcement is read and acknowledged.
                    StaffUI.header("Your manager")
                    messagesRow
                    StaffUI.header("Announcements")
                    switch inbox {
                    case .loading:
                        StaffUI.loading("Loading your inbox")
                    case .failed(let message):
                        StaffUI.failedCard(message) { Task { await reload() } }
                    case .loaded(let payload):
                        if payload.announcements.isEmpty {
                            CavnarEmptyHearth(title: "Nothing from your manager yet",
                                              message: "Announcements for the team land here. Tap Got it so your manager knows you read one.")
                        } else {
                            ForEach(payload.announcements) { announcementRow($0).id($0.id) }
                        }
                    }
                }
                .padding(20)
            }
            .cavnarEmberRefreshable { await reload() }
            .onChange(of: focusedAnnouncement) { _, id in
                guard let id else { return }
                withAnimation(.easeOut(duration: 0.3)) { proxy.scrollTo(id, anchor: .top) }
            }
            }
            .accountSheetChrome("Inbox")
        }
        .task { await reload() }
        .onChange(of: store.focus) { _, _ in takeFocus() }
        .onChange(of: scenePhase) { _, phase in if phase == .active { Task { await reload() } } }
        .sheet(isPresented: $messaging, onDismiss: { Task { await reload() } }) {
            StaffMessageThreadView(store: store).environment(staff)
        }
    }

    private var unreadMessages: Int { inbox.value?.unreadMessages ?? 0 }

    private var messagesRow: some View {
        Button {
            Haptic.light()
            messaging = true
        } label: {
            HStack(spacing: 12) {
                VStack(alignment: .leading, spacing: 3) {
                    Text("Messages")
                        .cavnarText(.label)
                    CavnarMixedText(unreadMessages > 0
                                    ? (unreadMessages == 1 ? "1 new reply" : "\(unreadMessages) new replies")
                                    : "Ask a question or follow up on a request. Your managers answer here.",
                                    role: .secondary)
                        .multilineTextAlignment(.leading)
                }
                Spacer(minLength: 8)
                if unreadMessages > 0 {
                    Text("\(unreadMessages)")
                        .font(.cavnarNumber(CavnarType.secondary, weight: 700))
                        .foregroundStyle(.white)
                        .padding(.horizontal, 8)
                        .padding(.vertical, 3)
                        .background(Color.cavnarRed, in: Capsule())
                }
                AccountDisclosureChip()
            }
            .frame(minHeight: 44)
            .contentShape(Rectangle())
        }
        .buttonStyle(.plain)
        .frame(maxWidth: .infinity, alignment: .leading)
        .cavnarCard()
        .accessibilityElement(children: .combine)
        .accessibilityLabel(unreadMessages > 0 ? "Messages, \(unreadMessages) unread" : "Messages")
        .accessibilityAddTraits(.isButton)
    }

    private func announcementRow(_ a: StaffAnnouncement) -> some View {
        VStack(alignment: .leading, spacing: 6) {
            if a.isUrgent {
                CavnarKicker("Urgent", tint: .cavnarRedText)
            }
            let text = a.shown(original: showingOriginal.contains(a.id))
            HomeMixedText.make(text.title, size: CavnarType.emphasis, weight: 700, color: .cavnarInk)
                .fixedSize(horizontal: false, vertical: true)
            if !text.body.isEmpty {
                CavnarMixedText(text.body, role: .body)
            }
            if a.translated {
                // Translated for this reader at delivery (S9): one tap shows
                // what the manager wrote, and back.
                let original = showingOriginal.contains(a.id)
                StaffTextButton(title: original ? "Show translation" : "Show original") {
                    if original { showingOriginal.remove(a.id) } else { showingOriginal.insert(a.id) }
                }
                .accessibilityHint(original ? "Shows this in your language" : "Shows what your manager wrote")
            }
            // The byline and "Got it" on one line: the ack is a right-
            // aligned 44pt ember text button, not a full-width bar per card.
            HStack(alignment: .center, spacing: CavnarSpace.xs) {
                VStack(alignment: .leading, spacing: 2) {
                    if !a.byline.isEmpty {
                        CavnarMixedText(a.byline, role: .caption)
                    }
                    if let until = a.expiresOn {
                        CavnarMixedText("Until \(CavnarDate.mdy(until))", role: .caption)
                    }
                }
                Spacer(minLength: CavnarSpace.xs)
                if a.isRead {
                    HStack(spacing: 6) {
                        Image(systemName: "checkmark").font(.cavnar(.caption).weight(.bold))
                        Text("Got it")
                    }
                    .font(.cavnar(.label))
                    .foregroundStyle(Color.cavnarGreen)
                    .frame(minHeight: 44)
                    .accessibilityElement(children: .combine)
                    .accessibilityLabel("You marked this read")
                } else {
                    Button {
                        Task { await ack(a.id) }
                    } label: {
                        Group {
                            if acking.contains(a.id) {
                                StaffShimmerLabel(text: "Saving", color: .cavnarEmber2)
                            } else {
                                Text("Got it")
                            }
                        }
                        .font(.cavnar(.label))
                        .foregroundStyle(Color.cavnarEmber2)
                        .padding(.horizontal, CavnarSpace.xs)
                        .cavnarHitTarget()
                    }
                    .buttonStyle(.plain)
                    .disabled(acking.contains(a.id))
                    .accessibilityHint("Tells your manager you read it.")
                }
            }
            if let e = ackError[a.id] { StaffUI.errorLine(e) }
        }
        .frame(maxWidth: .infinity, alignment: .leading)
        .cavnarCard()
        .overlay {
            if a.isUrgent && !a.isRead {
                RoundedRectangle(cornerRadius: CavnarRadius.card)
                    .strokeBorder(Color.cavnarRed.opacity(0.7), lineWidth: 1.5)
            }
        }
        // The one a notification opened, until it's read.
        .staffFocusRing(focusedAnnouncement == a.id && !a.isRead && !a.isUrgent)
        .accessibilityElement(children: .contain)
    }

    /// A push that opened the inbox (StaffPortalStore.focus): a manager's
    /// reply opens the thread; an announcement is scrolled to and ringed.
    /// Taken once, then consumed.
    private func takeFocus() {
        guard let link = store.focus, link.tab == .inbox else { return }
        if StaffInboxFocus.opensThread(link) {
            store.consumeFocus()
            messaging = true
            return
        }
        guard let id = link.itemID else { store.consumeFocus(); return }
        // Wait for the list, so the row exists to scroll to.
        guard inbox.value != nil else { return }
        store.consumeFocus()
        if inbox.value?.announcements.contains(where: { $0.id == id }) == true { focusedAnnouncement = id }
    }

    private func ack(_ id: Int) async {
        acking.insert(id)
        ackError[id] = nil
        defer { acking.remove(id) }
        do {
            let r: StaffAckResponse = try await staff.authed("/staff/api/announcements/\(id)/ack",
                                                             method: .post, body: StaffEmptyBody())
            Haptic.success()
            if let current = inbox.value {
                inbox = .loaded(current.acking(id, at: r.ackedAt ?? ISO8601DateFormatter().string(from: Date())))
            }
            await store.reloadBadges()
        } catch {
            ackError[id] = StaffErrorText.message(error)
        }
    }

    private func reload() async {
        do {
            inbox = .loaded(try await staff.authed("/staff/api/inbox"))
        } catch {
            if inbox.value == nil {
                inbox = .failed(StaffErrorText.message(error, fallback: "Your inbox didn\u{2019}t load."))
            }
        }
        takeFocus()
    }
}

/// How a staff link that opens the inbox lands (push.py's staff notices).
enum StaffInboxFocus {
    /// A manager's reply (staff_message / kind "message" / a thread id)
    /// opens the thread; anything else is an announcement.
    static func opensThread(_ link: StaffDeepLink) -> Bool {
        link.alertType == "staff_message" || link.kind == "message"
    }
}

// MARK: - The thread with the managers

/// The one thread with this restaurant's managers (any of them reads it
/// for the team), as a full sheet: optional context (a shift or one of my
/// requests) carried with the next message, a composer, and the posted
/// check on a send. Three ways in:
///   `StaffMessageThreadView(store: StaffPortalStore, shiftDate: String?)` — the container's (Today)
///   `StaffMessageThreadView(store: StaffPortalStore, context:draft:)`
///   `StaffMessageThreadView(context:draft:)` — from a screen without the portal store
/// `draft` pre-fills the box (Docs' "Ask your manager" passes the question).
/// Calls use the environment's StaffSessionStore.
struct StaffMessageThreadView: View {
    private let portal: StaffPortalStore?
    @State private var context: StaffMessageContext?
    @State private var text: String

    init(store: StaffPortalStore, shiftDate: String?) {
        self.init(portal: store, context: shiftDate.map { StaffMessageContext.shift(date: $0) }, draft: "")
    }

    init(store: StaffPortalStore, context: StaffMessageContext? = nil, draft: String = "") {
        self.init(portal: store, context: context, draft: draft)
    }

    init(context: StaffMessageContext? = nil, draft: String = "") {
        self.init(portal: nil, context: context, draft: draft)
    }

    private init(portal: StaffPortalStore?, context: StaffMessageContext?, draft: String) {
        self.portal = portal
        _context = State(initialValue: context)
        _text = State(initialValue: draft)
    }

    @Environment(StaffSessionStore.self) private var staff
    @State private var thread: StaffLoad<StaffThreadPayload> = .loading
    @State private var sending = false
    @State private var error: String?
    @State private var posted: String?
    @FocusState private var composing: Bool

    private static let limit = 1000

    var body: some View {
        NavigationStack {
            ScrollViewReader { proxy in
                ScrollView {
                    VStack(alignment: .leading, spacing: 12) {
                        switch thread {
                        case .loading:
                            StaffUI.loading("Loading your messages")
                        case .failed(let message):
                            StaffUI.failedCard(message) { Task { await reload() } }
                            StaffUI.note("You can still send a message below.")
                        case .loaded(let t):
                            if t.messages.isEmpty {
                                CavnarEmptyHearth(title: "No messages yet",
                                                  message: "Write to your managers here. Whoever is on reads it and answers in this thread.")
                            } else {
                                ForEach(t.messages) { bubble($0, seen: $0.id == t.lastSeenMineId) }
                            }
                        }
                        Color.clear.frame(height: 1).id("bottom")
                    }
                    .padding(20)
                }
                .onChange(of: thread.value?.messages.count ?? 0) { _, _ in
                    withAnimation(.easeOut(duration: 0.25)) { proxy.scrollTo("bottom", anchor: .bottom) }
                }
                .onAppear { proxy.scrollTo("bottom", anchor: .bottom) }
            }
            .safeAreaInset(edge: .bottom) { composer }
            .accountSheetChrome("Your manager")
        }
        .task { await reload() }
        .cavnarPostedOverlay(posted) { posted = nil }
    }

    private func bubble(_ m: StaffThreadMessage, seen: Bool) -> some View {
        VStack(alignment: m.isMine ? .trailing : .leading, spacing: 4) {
            if let about = m.contextLabel {
                CavnarMixedText(about, role: .caption)
            }
            Text(m.body)
                .cavnarText(.body, color: .cavnarInk)
                .fixedSize(horizontal: false, vertical: true)
                .padding(.horizontal, 14)
                .padding(.vertical, 10)
                .background(m.isMine ? Color.cavnarEmber.opacity(0.16) : Color.cavnarPaper2,
                            in: RoundedRectangle(cornerRadius: CavnarRadius.card))
                .overlay(RoundedRectangle(cornerRadius: CavnarRadius.card)
                    .strokeBorder(Color.cavnarPaper3.opacity(m.isMine ? 0 : 0.6), lineWidth: 1))
            CavnarMixedText([m.isMine ? "You" : m.senderName, m.timeLabel].filter { !$0.isEmpty }
                                .joined(separator: " · "), role: .caption)
            if seen {
                Text("Seen").cavnarText(.caption)
            }
        }
        .frame(maxWidth: .infinity, alignment: m.isMine ? .trailing : .leading)
        .padding(m.isMine ? .leading : .trailing, 36)
        .id(m.id)
        .accessibilityElement(children: .combine)
        .accessibilityLabel("\(m.isMine ? "You" : m.senderName): \(m.body). \(m.timeLabel)\(seen ? ". Seen" : "")")
    }

    private var composer: some View {
        VStack(alignment: .leading, spacing: 8) {
            if let context {
                HStack(spacing: 8) {
                    CavnarMixedText("About \(context.label)", role: .secondary)
                        .lineLimit(2)
                    Spacer(minLength: 4)
                    Button {
                        self.context = nil
                    } label: {
                        Image(systemName: "xmark")
                            .font(.cavnar(.caption).weight(.bold))
                            .foregroundStyle(Color.cavnarInk2)
                            .frame(width: 44, height: 44)
                            .contentShape(Rectangle())
                    }
                    .buttonStyle(.plain)
                    .accessibilityLabel("Don\u{2019}t attach \(context.label)")
                }
                .padding(.leading, 12)
                .background(Color.cavnarPaper2, in: RoundedRectangle(cornerRadius: CavnarRadius.control))
            }
            // The field and its send button on one line, the button 44pt
            // in thumb reach (a chat composer, not a form).
            HStack(alignment: .bottom, spacing: CavnarSpace.xs) {
                TextField("Message your manager", text: $text, axis: .vertical)
                    .lineLimit(1...5)
                    .focused($composing)
                    .cavnarTextFieldStyle()
                    .onChange(of: text) { _, v in if v.count > Self.limit { text = String(v.prefix(Self.limit)) } }
                sendButton
            }
            if let error { StaffUI.errorLine(error) }
        }
        .padding(.horizontal, 20)
        .padding(.top, 10)
        .padding(.bottom, 12)
        .background(Color.cavnarPaper.opacity(0.97))
    }

    /// The ember send disc beside the field — the Team thread's composer
    /// (TeamMessagesView): 44pt, paper until there is something to send,
    /// the shimmer line while sending.
    private var sendButton: some View {
        Button {
            Task { await send() }
        } label: {
            Group {
                if sending {
                    CavnarShimmerLine(color: .white).frame(width: 22)
                } else {
                    Image(systemName: "arrow.up").font(.cavnar(.figureS).weight(.bold))
                }
            }
            .foregroundStyle(Color.white)
            .frame(width: 44, height: 44)
            .background(Circle().fill(trimmed.isEmpty ? Color.cavnarPaper3 : Color.cavnarEmber))
            .contentShape(Circle())
        }
        .buttonStyle(.plain)
        .disabled(sending || trimmed.isEmpty)
        .accessibilityLabel(sending ? "Sending" : "Send")
    }

    private var trimmed: String { text.trimmingCharacters(in: .whitespacesAndNewlines) }

    private func reload() async {
        do {
            thread = .loaded(try await staff.authed("/staff/api/messages"))
            // Opening it reads the managers' replies: the inbox count drops.
            await portal?.reloadBadges()
        } catch {
            if thread.value == nil {
                thread = .failed(StaffErrorText.message(error, fallback: "Your messages didn\u{2019}t load."))
            }
        }
    }

    private func send() async {
        let body = trimmed
        guard !body.isEmpty else { return }
        sending = true
        error = nil
        defer { sending = false }
        do {
            let r: StaffMessageSent = try await staff.authed("/staff/api/messages", method: .post,
                                                            body: StaffMessageBody(body: body, context: context))
            Haptic.success()
            text = ""
            context = nil
            composing = false
            if let t = thread.value {
                thread = .loaded(t.appending(r.message))
            } else {
                await reload()
            }
            posted = "Sent to your manager"
        } catch {
            self.error = StaffErrorText.message(error)
        }
    }
}

// MARK: - Post-shift pulse (V6)

/// `StaffPulseCard()` or `StaffPulseCard(store: StaffPortalStore)` — after a
/// shift that has started (today's, or one of the last few days with no
/// answer yet), one tap 1–5 and an optional note. Shows nothing when
/// nothing is due or the read failed. Today can mount it under the hero;
/// the Inbox shows it at the top. Calls use the environment's session.
struct StaffPulseCard: View {
    private let portal: StaffPortalStore?
    init() { portal = nil }
    /// With the portal store, the card reads again on each portal refresh
    /// (pull, foreground), so one answered in the Inbox leaves Today too.
    init(store: StaffPortalStore) { portal = store }

    @Environment(StaffSessionStore.self) private var staff
    @State private var due: StaffPulseDue?
    @State private var rating: Int?
    @State private var note = ""
    @State private var sending = false
    @State private var error: String?
    @State private var thanked = false
    @State private var closed = false

    var body: some View {
        Group {
            if thanked {
                CavnarInlinePosted(label: "Thanks \u{2014} sent") { thanked = false }
            } else if let due, !closed {
                card(due)
            } else if closed, let error {
                StaffUI.note(error)
            }
        }
        .task(id: portal?.lastAttempt) {
            let r: StaffPulseState? = try? await staff.authed("/staff/api/pulse")
            due = r?.due
        }
    }

    private func card(_ due: StaffPulseDue) -> some View {
        VStack(alignment: .leading, spacing: 10) {
            Text("How did your shift go?")
                .font(.cavnarBody(CavnarType.emphasis, weight: 700))
                .foregroundStyle(Color.cavnarInk)
                .accessibilityAddTraits(.isHeader)
            CavnarMixedText(due.line, role: .secondary)
            HStack(spacing: 8) {
                ForEach(StaffPulseScale.ratings, id: \.self) { r in
                    Button {
                        Haptic.selection()
                        rating = r
                    } label: {
                        // The figure alone; the word only on the chosen one
                        // (five words never fit five chips at a readable size).
                        VStack(spacing: 2) {
                            Text("\(r)").font(.cavnar(.figureS))
                            if rating == r {
                                Text(StaffPulseScale.word(r)).font(.cavnar(.caption))
                                    .lineLimit(1)
                            }
                        }
                        .foregroundStyle(rating == r ? Color.cavnarInk : Color.cavnarInk2)
                        .frame(maxWidth: .infinity, minHeight: 52)
                        .background(rating == r ? Color.cavnarEmber.opacity(0.18) : Color.cavnarPaper3.opacity(0.45),
                                    in: RoundedRectangle(cornerRadius: CavnarRadius.control))
                        .overlay(RoundedRectangle(cornerRadius: CavnarRadius.control)
                            .strokeBorder(rating == r ? Color.cavnarEmber : Color.clear, lineWidth: 1.5))
                        .contentShape(Rectangle())
                    }
                    .buttonStyle(.plain)
                    .accessibilityLabel("\(r) of 5, \(StaffPulseScale.word(r))")
                    .accessibilityAddTraits(rating == r ? .isSelected : [])
                }
            }
            if rating == nil {
                CavnarMixedText("1 is \(StaffPulseScale.word(1).lowercased()), 5 is \(StaffPulseScale.word(5).lowercased())",
                                role: .caption)
                    .accessibilityHidden(true)
            }
            if rating != nil {
                TextField("Anything to add? (optional)", text: $note, axis: .vertical)
                    .lineLimit(1...3)
                    .cavnarTextFieldStyle()
                    .onChange(of: note) { _, v in if v.count > 300 { note = String(v.prefix(300)) } }
                Button {
                    Task { await send(due) }
                } label: {
                    Group {
                        if sending { StaffShimmerLabel(text: "Sending", color: .cavnarInk) } else { Text("Send") }
                    }
                    .frame(maxWidth: .infinity)
                }
                .buttonStyle(CavnarSecondaryButtonStyle(isDisabled: sending))
                .disabled(sending)
            }
            StaffUI.note("Your manager sees the team\u{2019}s answers together, never who said what.")
            if let error { StaffUI.errorLine(error) }
        }
        .frame(maxWidth: .infinity, alignment: .leading)
        .cavnarCard()
    }

    private func send(_ due: StaffPulseDue) async {
        guard let rating else { return }
        sending = true
        error = nil
        defer { sending = false }
        let n = note.trimmingCharacters(in: .whitespacesAndNewlines)
        do {
            let r: StaffOKResponse = try await staff.authed(
                "/staff/api/pulse", method: .post,
                body: StaffPulseBody(date: due.date, rating: rating, note: n.isEmpty ? nil : n))
            if r.ok {
                Haptic.success()
                closed = true
                thanked = true
            } else {
                error = r.error ?? "That didn\u{2019}t go through."
            }
        } catch let e as APIClient.APIError where e.status == 409 {
            // Already answered, or not a shift that can be rated: say so
            // and stop asking.
            error = e.message
            self.rating = nil
            closed = true
        } catch {
            self.error = StaffErrorText.message(error)
        }
    }
}
