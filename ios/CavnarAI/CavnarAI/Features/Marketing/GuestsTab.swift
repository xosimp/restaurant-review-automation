import Observation
import SwiftUI

// Marketing → Guests on the phone (guest_hub, 10/20/26): the web's Guests tab
// (dashboard.html `#mkt-tab-guests`) — the host stand's add (the reply-Y
// invite only when the guest asked), everyone who came in with their texts
// state, the next-day thank-you and guests' replies. One body with the web
// (/mobile/api/guests…). The spreadsheet import stays on the web: a CSV on
// a phone is a file most people can't reach.

// MARK: - Models

struct GuestCounts: Decodable, Hashable {
    var total = 0, yesterday = 0, joined = 0, waiting = 0, unread = 0
    enum CodingKeys: String, CodingKey { case total, yesterday, joined, waiting, unread }
    init() {}
    init(from decoder: Decoder) throws {
        let c = try decoder.container(keyedBy: CodingKeys.self)
        total = c.sfInt(.total) ?? 0; yesterday = c.sfInt(.yesterday) ?? 0
        joined = c.sfInt(.joined) ?? 0; waiting = c.sfInt(.waiting) ?? 0; unread = c.sfInt(.unread) ?? 0
    }
}

struct GuestRow: Decodable, Identifiable, Hashable {
    var id: Int
    var name: String
    var phone: String
    var lastVisit: String?
    var visits: Int
    var party: Int?
    var source: String
    var texts: String
    var usual: [String]
    enum CodingKeys: String, CodingKey {
        case id, name, phone, visits, party, source, texts, usual
        case lastVisit = "last_visit"
    }
    init(from decoder: Decoder) throws {
        let c = try decoder.container(keyedBy: CodingKeys.self)
        id = c.sfInt(.id) ?? 0
        name = c.sfText(.name) ?? ""
        phone = c.sfText(.phone) ?? ""
        lastVisit = c.sfText(.lastVisit)
        visits = c.sfInt(.visits) ?? 0
        party = c.sfInt(.party)
        source = c.sfText(.source) ?? ""
        texts = c.sfText(.texts) ?? "not_invited"
        usual = c.sfWords(.usual)
    }

    var textsLabel: String {
        switch texts {
        case "joined": return "Joined"
        case "waiting": return "Waiting for Y"
        case "stopped": return "Stopped"
        default: return "Not invited"
        }
    }

    var textsTone: CavnarTone {
        switch texts {
        case "joined": return .good
        case "waiting": return .warning
        case "stopped": return .bad
        default: return .neutral
        }
    }
}

struct GuestFollowup: Decodable, Hashable {
    var enabled = false
    var message = ""
    var reviewLink = false
    var canReviewLink = false
    var prefix = ""
    enum CodingKeys: String, CodingKey {
        case enabled, message, prefix
        case reviewLink = "review_link"
        case canReviewLink = "can_review_link"
    }
    init() {}
    init(from decoder: Decoder) throws {
        let c = try decoder.container(keyedBy: CodingKeys.self)
        enabled = c.sfBool(.enabled) ?? false
        message = c.sfText(.message) ?? ""
        reviewLink = c.sfBool(.reviewLink) ?? false
        canReviewLink = c.sfBool(.canReviewLink) ?? false
        prefix = c.sfText(.prefix) ?? ""
    }
}

struct GuestsPayload: Decodable {
    var counts = GuestCounts()
    var guests: [GuestRow] = []
    var more = 0
    var followup = GuestFollowup()
    var ownNumber: String?
    var tockConnected = false
    var tockEvents = 0
    var tockLast: String?
    var canPublish = false
    enum CodingKeys: String, CodingKey { case counts, guests, more, followup, texting, tock
        case canPublish = "can_publish" }
    enum TextingKeys: String, CodingKey { case number
        case ownNumber = "own_number" }
    enum TockKeys: String, CodingKey { case connected, events
        case lastEvent = "last_event" }
    init(from decoder: Decoder) throws {
        let c = try decoder.container(keyedBy: CodingKeys.self)
        counts = (try? c.decodeIfPresent(GuestCounts.self, forKey: .counts)) ?? GuestCounts()
        guests = ((try? c.decodeIfPresent(HomeLenientListDecodable<GuestRow>.self, forKey: .guests)) ?? nil)?.items ?? []
        more = c.sfInt(.more) ?? 0
        followup = (try? c.decodeIfPresent(GuestFollowup.self, forKey: .followup)) ?? GuestFollowup()
        canPublish = c.sfBool(.canPublish) ?? false
        if let t = try? c.nestedContainer(keyedBy: TextingKeys.self, forKey: .texting),
           t.sfBool(.ownNumber) == true {
            ownNumber = t.sfText(.number)
        }
        if let k = try? c.nestedContainer(keyedBy: TockKeys.self, forKey: .tock) {
            tockConnected = k.sfBool(.connected) ?? false
            tockEvents = k.sfInt(.events) ?? 0
            tockLast = k.sfText(.lastEvent)
        }
    }
}

struct GuestThread: Decodable, Identifiable, Hashable {
    var phone: String
    var display: String
    var name: String
    var unread: Int
    var last: String
    var id: String { phone }
    enum CodingKeys: String, CodingKey { case phone, display, name, unread, last }
    init(from decoder: Decoder) throws {
        let c = try decoder.container(keyedBy: CodingKeys.self)
        phone = c.sfText(.phone) ?? ""
        display = c.sfText(.display) ?? phone
        name = c.sfText(.name) ?? ""
        unread = c.sfInt(.unread) ?? 0
        last = c.sfText(.last) ?? ""
    }
}

struct GuestMessage: Decodable, Identifiable, Hashable {
    var direction: String
    var body: String
    var at: String
    var id: String { at + direction + body.prefix(20) }
    enum CodingKeys: String, CodingKey { case direction, body, at }
    init(from decoder: Decoder) throws {
        let c = try decoder.container(keyedBy: CodingKeys.self)
        direction = c.sfText(.direction) ?? "in"
        body = c.sfText(.body) ?? ""
        at = c.sfText(.at) ?? ""
    }
}

enum GuestsView: String, CaseIterable, Identifiable {
    case all = "All", yesterday = "Yesterday", joined = "Joined", waiting = "Waiting"
    var id: String { rawValue }
    var key: String { rawValue.lowercased() }
}

// MARK: - View model

@Observable
@MainActor
final class GuestsTabViewModel {
    var payload: GuestsPayload?
    var threads: [GuestThread] = []
    var view: GuestsView = .all
    var query = ""
    var isLoading = false
    var loadError: String?
    var hostNotice: String?
    var hostNoticeIsError = false
    var isAdding = false
    var followNotice: String?
    var isSavingFollow = false
    private(set) var lastLoadedAt: Date?

    private let client: APIClient

    init(client: APIClient = .shared) {
        self.client = client
    }

    private struct ThreadsResponse: Decodable {
        let threads: [GuestThread]
        init(from decoder: Decoder) throws {
            let c = try decoder.container(keyedBy: CodingKeys.self)
            threads = ((try? c.decodeIfPresent(HomeLenientListDecodable<GuestThread>.self, forKey: .threads)) ?? nil)?.items ?? []
        }
        enum CodingKeys: String, CodingKey { case threads }
    }

    private struct MessagesResponse: Decodable {
        let messages: [GuestMessage]
        init(from decoder: Decoder) throws {
            let c = try decoder.container(keyedBy: CodingKeys.self)
            messages = ((try? c.decodeIfPresent(HomeLenientListDecodable<GuestMessage>.self, forKey: .messages)) ?? nil)?.items ?? []
        }
        enum CodingKeys: String, CodingKey { case messages }
    }

    private struct AddResponse: Decodable {
        let ok: Bool
        let error: String?
        let invited: Bool?
        let inviteError: String?
        enum CodingKeys: String, CodingKey { case ok, error, invited
            case inviteError = "invite_error" }
    }

    private struct HostAdd: Encodable {
        let name: String, phone: String, party: Int?, invite: Bool, asked: Bool
    }

    private struct FollowSave: Encodable {
        let enabled: Bool, message: String
        let review_link: Bool
    }

    private struct ReplyBody: Encodable { let phone: String, body: String }

    func load() async {
        isLoading = true
        defer { isLoading = false }
        async let p: GuestsPayload? = try? client.send("/mobile/api/guests", query: ["view": view.key, "q": query],
                                                       hapticOnError: false)
        async let t: ThreadsResponse? = try? client.send("/mobile/api/guests/inbox", hapticOnError: false)
        let (pay, th) = await (p, t)
        if let pay { payload = pay; lastLoadedAt = Date() }
        if let th { threads = th.threads }
        loadError = (pay == nil && payload == nil) ? "Couldn\u{2019}t load your guests. Pull to try again." : nil
    }

    /// The host stand's add. The invite goes only when the guest asked for
    /// texts — `asked` is the host's own confirmation, the same checkbox as
    /// the web.
    func add(name: String, phone: String, party: Int?, invite: Bool) async -> Bool {
        isAdding = true
        defer { isAdding = false }
        do {
            let r: AddResponse = try await client.send("/mobile/api/guests/host-add", method: .post,
                                                       body: HostAdd(name: name, phone: phone, party: party,
                                                                     invite: invite, asked: invite))
            guard r.ok else {
                hostNotice = r.error ?? "That guest wasn\u{2019}t saved. Try again."
                hostNoticeIsError = true
                return false
            }
            let who = name.split(separator: " ").first.map(String.init) ?? "The guest"
            if invite && r.invited == true {
                hostNotice = "\(who) is added. The invite went out; they reply Y to join."
            } else if invite {
                hostNotice = "\(who) is added. The invite didn\u{2019}t send: \(r.inviteError ?? "try again later")"
            } else {
                hostNotice = "\(who) is added."
            }
            hostNoticeIsError = invite && r.invited != true
            await load()
            return true
        } catch {
            hostNotice = (error as? LocalizedError)?.errorDescription ?? "That guest wasn\u{2019}t saved. Check your connection."
            hostNoticeIsError = true
            return false
        }
    }

    func saveFollowup(enabled: Bool, message: String, reviewLink: Bool) async {
        isSavingFollow = true
        defer { isSavingFollow = false }
        do {
            let f: GuestFollowup = try await client.send("/mobile/api/guests/followup", method: .post,
                                                         body: FollowSave(enabled: enabled, message: message,
                                                                          review_link: reviewLink))
            payload?.followup = f
            followNotice = f.enabled ? "Saved. It goes out at 11am." : "Saved. It\u{2019}s off."
        } catch {
            followNotice = (error as? LocalizedError)?.errorDescription ?? "Not saved. Try again."
        }
    }

    func messages(for phone: String) async -> [GuestMessage] {
        let r: MessagesResponse? = try? await client.send("/mobile/api/guests/inbox", query: ["phone": phone],
                                                         hapticOnError: false)
        return r?.messages ?? []
    }

    func reply(to phone: String, body: String) async -> String? {
        do {
            let r: APIClient.OKResponse = try await client.send("/mobile/api/guests/inbox", method: .post,
                                                                body: ReplyBody(phone: phone, body: body))
            return r.ok ? nil : (r.error ?? "That reply didn\u{2019}t send.")
        } catch {
            return (error as? LocalizedError)?.errorDescription ?? "That reply didn\u{2019}t send."
        }
    }
}

// MARK: - The section

struct GuestsTabSection: View {
    let viewModel: GuestsTabViewModel

    @State private var name = ""
    @State private var phone = ""
    @State private var party: Int?
    @State private var asked = false
    @State private var followOn = false
    @State private var followMessage = ""
    @State private var followReview = false
    @State private var followLoaded = false
    @State private var openThread: GuestThread?
    @State private var searchTask: Task<Void, Never>?

    var body: some View {
        VStack(alignment: .leading, spacing: CavnarSpace.l) {
            if let error = viewModel.loadError, viewModel.payload == nil {
                Text(error).cavnarText(.body, color: .cavnarRedText)
            }
            if viewModel.payload == nil && viewModel.isLoading {
                CavnarSkeletonLines(widths: [1.0, 0.8, 0.9, 0.6]).padding(.vertical, 12)
            }
            countsRow
            hostCard
            listSection
            followCard
            repliesCard
            footnotes
        }
        .onChange(of: viewModel.payload?.followup) { _, f in
            guard let f, !followLoaded else { return }
            followLoaded = true
            followOn = f.enabled; followMessage = f.message; followReview = f.reviewLink
        }
        .sheet(item: $openThread) { t in
            GuestThreadSheet(thread: t, viewModel: viewModel)
        }
    }

    // MARK: Counts

    private var countsRow: some View {
        let c = viewModel.payload?.counts
        return LazyVGrid(columns: [GridItem(.flexible(), spacing: CavnarSpace.s), GridItem(.flexible())],
                         spacing: CavnarSpace.s) {
            countTile("Came in yesterday", c?.yesterday)
            countTile("Joined texts", c?.joined)
            countTile("Waiting for their Y", c?.waiting)
            countTile("All guests", c?.total)
        }
    }

    private func countTile(_ label: String, _ value: Int?) -> some View {
        VStack(alignment: .leading, spacing: CavnarSpace.xs) {
            Text(value.map { "\($0)" } ?? "\u{2014}").cavnarText(.figureL, color: value == nil ? .cavnarInk3 : .cavnarInk)
            Text(label).cavnarText(.secondary).lineLimit(1).minimumScaleFactor(0.85)
        }
        .frame(maxWidth: .infinity, alignment: .leading)
        .cavnarCard()
        .accessibilityElement(children: .combine)
    }

    // MARK: Host stand

    private var hostCard: some View {
        VStack(alignment: .leading, spacing: CavnarSpace.s) {
            CavnarKicker("Host stand")
            Text("Add a guest").cavnarText(.headline)
            TextField("Name", text: $name)
                .textContentType(.name)
                .cavnarTextFieldStyle()
            TextField("Phone", text: $phone)
                .keyboardType(.phonePad)
                .textContentType(.telephoneNumber)
                .cavnarTextFieldStyle()
            Picker("Party", selection: $party) {
                Text("Party size").tag(Int?.none)
                ForEach(1...20, id: \.self) { Text("\($0)").tag(Int?.some($0)) }
            }
            .pickerStyle(.menu)
            .tint(Color.cavnarInk)
            .frame(minHeight: 44)
            Toggle(isOn: $asked) {
                VStack(alignment: .leading, spacing: 2) {
                    Text("The guest asked for texts").cavnarText(.label)
                    Text("Sends the invite now; they reply Y to join.").cavnarText(.caption)
                }
            }
            .tint(Color.cavnarEmber)
            Button {
                Haptic.light()
                Task {
                    if await viewModel.add(name: name, phone: phone, party: party, invite: asked) {
                        name = ""; phone = ""; party = nil; asked = false
                    }
                }
            } label: {
                Group {
                    if viewModel.isAdding {
                        CavnarShimmerText(text: "Adding\u{2026}", color: .white)
                    } else {
                        Text("Add guest")
                    }
                }
                .frame(maxWidth: .infinity)
            }
            .buttonStyle(CavnarPrimaryButtonStyle(isDisabled: viewModel.isAdding || phone.filter(\.isNumber).count < 10))
            .disabled(viewModel.isAdding || phone.filter(\.isNumber).count < 10)
            if let note = viewModel.hostNotice {
                Text(note).cavnarText(.secondary, color: viewModel.hostNoticeIsError ? .cavnarRedText : .cavnarGreen)
                    .fixedSize(horizontal: false, vertical: true)
            }
        }
        .cavnarCard()
    }

    // MARK: The list

    private var listSection: some View {
        VStack(alignment: .leading, spacing: CavnarSpace.s) {
            CavnarKicker("Guests")
            CavnarSegmentedControl(selection: Binding(get: { viewModel.view },
                                                      set: { v in viewModel.view = v; Task { await viewModel.load() } }),
                                   options: GuestsView.allCases, accessibilityTitle: "Show") { $0.rawValue }
            TextField("Search a name or number", text: Binding(get: { viewModel.query }, set: { q in
                viewModel.query = q
                searchTask?.cancel()
                searchTask = Task {
                    try? await Task.sleep(nanoseconds: 300_000_000)
                    if !Task.isCancelled { await viewModel.load() }
                }
            }))
            .cavnarTextFieldStyle()
            let rows = viewModel.payload?.guests ?? []
            if rows.isEmpty, viewModel.payload != nil {
                Text(viewModel.view == .all && viewModel.query.isEmpty
                     ? "No guests yet. Add one at the host stand."
                     : "No guests match.")
                    .cavnarText(.secondary)
                    .padding(.vertical, CavnarSpace.s)
            }
            VStack(spacing: 0) {
                ForEach(Array(rows.enumerated()), id: \.element.id) { i, g in
                    if i > 0 { Divider().overlay(Color.cavnarInk3.opacity(0.2)) }
                    guestRow(g)
                }
            }
            if let more = viewModel.payload?.more, more > 0 {
                Text("\(more) more. Search to find a guest.").cavnarText(.caption)
            }
        }
        .cavnarCard()
    }

    private func guestRow(_ g: GuestRow) -> some View {
        HStack(alignment: .top, spacing: CavnarSpace.s) {
            VStack(alignment: .leading, spacing: 3) {
                Text(g.name.isEmpty ? "Guest" : g.name).cavnarText(.label)
                HomeMixedText.make(g.phone + (g.lastVisit.map { " \u{00B7} \($0)" } ?? "")
                                   + " \u{00B7} \(g.visits) visit\(g.visits == 1 ? "" : "s")"
                                   + (g.party.map { " \u{00B7} party of \($0)" } ?? ""), role: .caption)
                if !g.usual.isEmpty {
                    Text("Usual: " + g.usual.joined(separator: ", ")).cavnarText(.caption)
                        .fixedSize(horizontal: false, vertical: true)
                }
            }
            Spacer(minLength: CavnarSpace.xs)
            TonePill(text: g.textsLabel, tone: g.textsTone)
        }
        .padding(.vertical, CavnarSpace.s)
        .accessibilityElement(children: .combine)
    }

    // MARK: Next-day thank-you

    private var followCard: some View {
        let f = viewModel.payload?.followup ?? GuestFollowup()
        let canPublish = viewModel.payload?.canPublish ?? false
        return VStack(alignment: .leading, spacing: CavnarSpace.s) {
            HStack {
                CavnarKicker("Next-day thank-you")
                Spacer()
                Toggle("", isOn: $followOn)
                    .labelsHidden()
                    .tint(Color.cavnarEmber)
                    .disabled(!canPublish)
                    .accessibilityLabel("Send the next-day thank-you")
            }
            Text("At 11am, to guests who came in yesterday and joined texts.").cavnarText(.secondary)
                .fixedSize(horizontal: false, vertical: true)
            TextField("Message", text: $followMessage, axis: .vertical)
                .lineLimit(3...5)
                .cavnarTextFieldStyle()
                .disabled(!canPublish)
            if f.canReviewLink {
                Toggle(isOn: $followReview) { Text("Add the Google review link").cavnarText(.label) }
                    .tint(Color.cavnarEmber)
                    .disabled(!canPublish)
            }
            Text(previewText(f))
                .cavnarText(.secondary, color: .cavnarInk)
                .padding(CavnarSpace.s)
                .background(RoundedRectangle(cornerRadius: 14).fill(Color.white.opacity(0.05)))
                .fixedSize(horizontal: false, vertical: true)
                .accessibilityLabel("What guests get: " + previewText(f))
            if canPublish {
                Button {
                    Haptic.light()
                    Task { await viewModel.saveFollowup(enabled: followOn, message: followMessage, reviewLink: followReview) }
                } label: {
                    Text(viewModel.isSavingFollow ? "Saving\u{2026}" : "Save").frame(maxWidth: .infinity)
                }
                .buttonStyle(CavnarPrimaryButtonStyle(isDisabled: viewModel.isSavingFollow))
                .disabled(viewModel.isSavingFollow)
            } else {
                Text("Only the owner can change the thank-you.").cavnarText(.caption)
            }
            if let n = viewModel.followNotice { Text(n).cavnarText(.caption) }
        }
        .cavnarCard()
    }

    private func previewText(_ f: GuestFollowup) -> String {
        let msg = followMessage.split(whereSeparator: \.isWhitespace).joined(separator: " ")
        var t = f.prefix + msg
        if followReview && f.canReviewLink { t += "\nReview us: (your Google review link)" }
        return t + "\n\nReply STOP to unsubscribe."
    }

    // MARK: Replies

    private var repliesCard: some View {
        VStack(alignment: .leading, spacing: CavnarSpace.s) {
            HStack {
                CavnarKicker("Replies")
                Spacer()
                if let u = viewModel.payload?.counts.unread, u > 0 { TonePill(text: "\(u) new", tone: .warning) }
            }
            if viewModel.threads.isEmpty {
                Text("Guests\u{2019} replies to your number show up here.").cavnarText(.secondary)
            }
            ForEach(viewModel.threads) { t in
                Button {
                    Haptic.light()
                    openThread = t
                } label: {
                    HStack(spacing: CavnarSpace.s) {
                        VStack(alignment: .leading, spacing: 2) {
                            Text(t.name.isEmpty ? t.display : t.name).cavnarText(.label)
                            Text(t.last).cavnarText(.caption).lineLimit(1)
                        }
                        Spacer()
                        if t.unread > 0 { TonePill(text: "\(t.unread)", tone: .warning) }
                        Image(systemName: "chevron.right").font(.cavnar(.caption)).foregroundStyle(Color.cavnarInk3)
                    }
                    .frame(minHeight: 44)
                    .contentShape(Rectangle())
                }
                .buttonStyle(.plain)
            }
        }
        .cavnarCard()
    }

    @ViewBuilder
    private var footnotes: some View {
        if let p = viewModel.payload {
            VStack(alignment: .leading, spacing: CavnarSpace.xs) {
                Text(p.ownNumber.map { "Texts come from \($0), and replies come back here." }
                     ?? "Texts come from the Cavnar AI number until your own number is approved.")
                Text(p.tockConnected
                     ? (p.tockEvents > 0 ? "Tock is connected: \(p.tockEvents) updates, the last on \(p.tockLast ?? "\u{2014}")."
                                         : "Tock is connected. Nothing has arrived yet.")
                     : "Tock isn\u{2019}t connected yet.")
                Text("Bring in your guest spreadsheet from Marketing \u{2192} Guests on the web.")
            }
            .cavnarText(.caption)
            .fixedSize(horizontal: false, vertical: true)
        }
    }
}

// MARK: - One guest's texts

struct GuestThreadSheet: View {
    let thread: GuestThread
    let viewModel: GuestsTabViewModel

    @Environment(\.dismiss) private var dismiss
    @State private var messages: [GuestMessage] = []
    @State private var draft = ""
    @State private var sending = false
    @State private var error: String?

    var body: some View {
        NavigationStack {
            VStack(spacing: 0) {
                ScrollView {
                    VStack(spacing: CavnarSpace.s) {
                        ForEach(messages) { m in
                            HStack {
                                if m.direction == "out" { Spacer(minLength: 40) }
                                Text(m.body)
                                    .cavnarText(.body)
                                    .padding(.horizontal, 12).padding(.vertical, 9)
                                    .background(RoundedRectangle(cornerRadius: 16)
                                        .fill(m.direction == "out" ? Color.cavnarEmber.opacity(0.18) : Color.white.opacity(0.06)))
                                if m.direction != "out" { Spacer(minLength: 40) }
                            }
                        }
                    }
                    .padding(CavnarSpace.gutter)
                }
                if let error { Text(error).cavnarText(.caption, color: .cavnarRedText).padding(.horizontal) }
                HStack(spacing: CavnarSpace.s) {
                    TextField("Reply", text: $draft, axis: .vertical)
                        .lineLimit(1...4)
                        .cavnarTextFieldStyle()
                    Button {
                        Haptic.light()
                        Task {
                            sending = true
                            error = await viewModel.reply(to: thread.phone, body: draft)
                            sending = false
                            if error == nil {
                                draft = ""
                                messages = await viewModel.messages(for: thread.phone)
                            }
                        }
                    } label: {
                        Image(systemName: "arrow.up").font(.cavnar(.label))
                            .frame(width: 44, height: 44)
                    }
                    .buttonStyle(CavnarPrimaryButtonStyle(isDisabled: sending || draft.trimmingCharacters(in: .whitespaces).isEmpty))
                    .disabled(sending || draft.trimmingCharacters(in: .whitespaces).isEmpty)
                    .accessibilityLabel("Send reply")
                }
                .padding(CavnarSpace.gutter)
            }
            .cavnarModuleBackground()
            .navigationTitle(thread.name.isEmpty ? thread.display : thread.name)
            .navigationBarTitleDisplayMode(.inline)
            .toolbar { ToolbarItem(placement: .cancellationAction) { Button("Done") { dismiss() } } }
            .task {
                messages = await viewModel.messages(for: thread.phone)
                await viewModel.load()
            }
        }
    }
}
