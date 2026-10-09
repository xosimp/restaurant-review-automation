import Charts
import Observation
import SwiftUI

/// One row of "What went out": a text campaign or a newsletter, newest
/// first across both.
enum CampaignHistoryItem: Identifiable {
    case text(GuestCampaign)
    case email(GuestNewsletter)

    var id: String {
        switch self {
        case .text(let c): return "t\(c.id)"
        case .email(let n): return "e\(n.id)"
        }
    }

    var createdAt: String {
        switch self {
        case .text(let c): return c.createdAt ?? ""
        case .email(let n): return n.createdAt ?? ""
        }
    }
}

/// Marketing → Campaigns on the phone: who's listening (the KPI strip and
/// twelve weeks of opt-ins), a way into the Studio, what went out (texts
/// and emails, with Stop sending and Retry for a login that may send) and the rules
/// every campaign follows (with the review-link invite switch). The web's
/// Campaigns tab (dashboard.html `#mkt-tab-campaigns`).
@Observable
@MainActor
final class CampaignsTabViewModel {
    var overview: GuestOverview?
    var campaigns: [GuestCampaign] = []
    var ledger: ConsentLedger?
    var newsletters: [GuestNewsletter] = []
    var winback: GuestWinback.Draft?
    var winbackDismissed = false
    var invites: OptinInvitesState?
    var invitesBusy = false
    var invitesError: String?
    var isLoading = false
    var loadError: String?
    /// The last follow-up's outcome ("12 texts won't go out").
    var notice: String?
    var actionError: String?
    var busyIDs: Set<String> = []
    private(set) var lastLoadedAt: Date?

    private let client: APIClient

    init(client: APIClient = .shared) {
        self.client = client
    }

    private struct HistoryResponse: Decodable {
        let campaigns: [GuestCampaign]
        let ledger: ConsentLedger?
        init(from decoder: Decoder) throws {
            let c = try decoder.container(keyedBy: CodingKeys.self)
            campaigns = ((try? c.decodeIfPresent(HomeLenientListDecodable<GuestCampaign>.self, forKey: .campaigns)) ?? nil)?.items ?? []
            ledger = (try? c.decodeIfPresent(ConsentLedger.self, forKey: .ledger)) ?? nil
        }
        enum CodingKeys: String, CodingKey { case campaigns, ledger }
    }

    private struct NewslettersResponse: Decodable {
        let newsletters: [GuestNewsletter]
        init(from decoder: Decoder) throws {
            let c = try decoder.container(keyedBy: CodingKeys.self)
            newsletters = ((try? c.decodeIfPresent(HomeLenientListDecodable<GuestNewsletter>.self, forKey: .newsletters)) ?? nil)?.items ?? []
        }
        enum CodingKeys: String, CodingKey { case newsletters }
    }

    /// Every part read at once; a part that fails keeps what is on screen.
    func load() async {
        isLoading = true
        defer { isLoading = false }
        async let ov: GuestOverview? = try? client.send("/mobile/api/guest-overview", hapticOnError: false)
        async let hist: HistoryResponse? = try? client.send("/mobile/api/guest-campaigns", hapticOnError: false)
        async let mail: NewslettersResponse? = try? client.send("/mobile/api/guest-newsletters", hapticOnError: false)
        async let wb: GuestWinback? = try? client.send("/mobile/api/guest-winback", hapticOnError: false)
        async let inv: OptinInvitesState? = try? client.send("/mobile/api/guest-optin-invites", hapticOnError: false)
        let (o, h, m, w, i) = await (ov, hist, mail, wb, inv)
        if let o { overview = o }
        if let h { campaigns = h.campaigns; ledger = h.ledger ?? ledger }
        if let m { newsletters = m.newsletters }
        if let w { winback = (w.ok && w.available == true) ? w.draft : nil }
        if let i, i.ok { invites = i }
        loadError = (o == nil && h == nil)
            ? "Couldn\u{2019}t load your campaigns. Pull to try again." : nil
        if o != nil || h != nil { lastLoadedAt = Date() }
    }

    /// Texts and emails, one list, newest first.
    var history: [CampaignHistoryItem] {
        let items = campaigns.map(CampaignHistoryItem.text) + newsletters.map(CampaignHistoryItem.email)
        return items.sorted { $0.createdAt > $1.createdAt }
    }

    /// The best text by taps per text, once two carried a link to 10+.
    var bestTextID: Int? {
        let linked = campaigns.filter { $0.linkToken != nil && $0.sentCount >= 10 }
        guard linked.count >= 2 else { return nil }
        return linked.max { Double($0.clicks) / Double($0.sentCount) < Double($1.clicks) / Double($1.sentCount) }?.id
    }

    /// "31 texted this month · 18 emailed · 2 texts failed · 98% accepted by
    /// carrier" — accepted, never "delivered" (CS-13). A month with no
    /// figure from the server says "—", never 0; an email counts in the
    /// restaurant's own month, not UTC's (re-audit 10/8/26).
    var monthLine: String? {
        guard let o = overview else { return nil }
        var bits = [(o.textsThisMonth.map { "\($0)" } ?? "\u{2014}") + " texted this month"]
        let month = Self.monthKey(Date())
        let mailed = newsletters.filter { Self.localMonth(of: $0.createdAt) == month }.reduce(0) { $0 + $1.sent }
        if mailed > 0 { bits.append("\(mailed) emailed") }
        if let f = o.textsFailedThisMonth, f > 0 { bits.append("\(mktPlural(f, "text")) failed") }
        if let a = o.accepted {
            bits.append((a == a.rounded() ? "\(Int(a))" : String(format: "%.1f", a)) + "% accepted by carrier")
        }
        return bits.joined(separator: " \u{00B7} ")
    }

    /// "2026-10" for a server stamp, in the restaurant's zone: a UTC
    /// "2026-11-01 02:00:00" is still October in Chicago.
    static func localMonth(of stamp: String?) -> String {
        guard let stamp, !stamp.isEmpty else { return "" }
        if let date = CavnarDate.timestamp(stamp) { return monthKey(date) }
        return String(stamp.prefix(7))
    }

    static func monthKey(_ date: Date) -> String {
        var cal = Calendar(identifier: .gregorian)
        cal.timeZone = RestaurantClock.timeZone
        let c = cal.dateComponents([.year, .month], from: date)
        return String(format: "%04d-%02d", c.year ?? 0, c.month ?? 0)
    }

    // MARK: Actions

    /// Stop a text campaign still sending or waiting for the window: its
    /// pending texts never go. Confirmed by the caller first.
    func stop(_ campaign: GuestCampaign) async {
        let key = "t\(campaign.id)"
        busyIDs.insert(key)
        defer { busyIDs.remove(key) }
        actionError = nil
        do {
            let r: CampaignSendResult = try await client.send("/mobile/api/guest-campaign/\(campaign.id)/cancel",
                                                              method: .post, retryTransient: false)
            if r.ok {
                Haptic.success()
                notice = "\(mktPlural(r.cancelled ?? 0, "text")) won\u{2019}t go out"
            } else {
                actionError = r.error ?? "Couldn\u{2019}t stop it."
            }
        } catch let error as APIClient.APIError {
            actionError = error.decodeBody(CampaignSendResult.self)?.error ?? error.message
        } catch {
            actionError = "Couldn\u{2019}t stop it."
        }
        await load()
    }

    /// Retry the failures a retry can reach. Confirmed by the caller first.
    /// "Send to the new subscribers" is not offered here: like the web, it
    /// rides only the same-day "Already sent" answer in the Studio.
    func retry(_ newsletter: GuestNewsletter) async {
        let key = "e\(newsletter.id)"
        busyIDs.insert(key)
        defer { busyIDs.remove(key) }
        actionError = nil
        let r = await CampaignMail.followUp(client: client, newsletterId: newsletter.id, retry: true)
        if r.ok {
            Haptic.success()
            notice = r.summary
        } else {
            actionError = r.error ?? "Couldn\u{2019}t send."
        }
        await load()
    }

    /// The review-link invite switch: on is the owner's acknowledgement of
    /// the disclosure beside it, stored with who and when (MB-3).
    func setInvites(_ on: Bool) async {
        invitesBusy = true
        invitesError = nil
        defer { invitesBusy = false }
        do {
            let r: OptinInvitesState = try await client.send(
                "/mobile/api/guest-optin-invites", method: .post,
                body: OptinInvitesBody(enabled: on, acknowledged: on), retryTransient: false)
            if r.ok { invites = r; Haptic.success() } else { invitesError = r.error ?? "Couldn\u{2019}t save that." }
        } catch let error as APIClient.APIError {
            invitesError = error.message
        } catch {
            invitesError = "Couldn\u{2019}t save that."
        }
    }

    private struct WinbackDismissBody: Encodable {
        let kind: String
        let reasonCode: String?
        enum CodingKeys: String, CodingKey { case kind; case reasonCode = "reason_code" }
    }

    /// "Not for us" on the win-back, after the reason picker.
    func dismissWinback(reasonCode: String?) async {
        guard let w = winback else { return }
        do {
            let r: APIClient.OKResponse = try await client.send(
                "/mobile/api/guest-winback/\(w.id)/dismiss", method: .post,
                body: WinbackDismissBody(kind: "not_for_us", reasonCode: reasonCode), retryTransient: false)
            if r.ok { winbackDismissed = true; Haptic.success() } else { actionError = r.error }
        } catch let error as APIClient.APIError {
            actionError = error.message
        } catch {
            actionError = "Couldn\u{2019}t save that."
        }
    }
}

/// "New campaign" is the Create card's primary unless a win-back
/// suggestion leads the card, whose "Use this" is then (re-audit L6).
private struct CampaignCreateButtonStyle: ViewModifier {
    let primary: Bool
    func body(content: Content) -> some View {
        if primary {
            content.buttonStyle(CavnarPrimaryButtonStyle())
        } else {
            content.buttonStyle(CavnarSecondaryButtonStyle())
        }
    }
}

struct CampaignsTabSection: View {
    let viewModel: CampaignsTabViewModel
    /// Whether this login may send, stop or retry (the server's
    /// may_publish, `can_publish` on the overview). A login that can't
    /// isn't shown the buttons the server would refuse it.
    var canPublish: Bool
    /// Whether this login may turn the invite texts on or off (the
    /// server's `can_change` — owner and client roles only, not an admin).
    var canChangeInvites: Bool
    var onOpenStudio: (StudioSeed) -> Void
    var onOpenTextClub: () -> Void

    @State private var rowHeights: [String: CGFloat] = [:]
    @State private var stopping: GuestCampaign?
    @State private var retrying: GuestNewsletter?
    @State private var askingWinbackReason = false
    @State private var showingDisclosure = false
    /// "Rules & consent" — the rules every campaign follows and the invite
    /// switch, one tap away rather than seven rows on the tab.
    @State private var showingRules = false

    var body: some View {
        VStack(alignment: .leading, spacing: CavnarSpace.l) {
            if let error = viewModel.loadError, viewModel.overview == nil {
                Text(error).cavnarText(.body, color: .cavnarRedText)
            }
            if viewModel.overview == nil && viewModel.isLoading {
                CavnarSkeletonLines(widths: [1.0, 0.8, 0.9, 0.6]).padding(.vertical, 12)
            }
            headlineRow
            insights
            createCard
            historySection
            rulesAndClubRows
        }
        .confirmationDialog(stopping.map { "Stop sending? \(mktPlural($0.pending, "text")) won\u{2019}t go out." } ?? "",
                            isPresented: Binding(get: { stopping != nil }, set: { if !$0 { stopping = nil } }),
                            titleVisibility: .visible, presenting: stopping) { c in
            Button("Stop sending", role: .destructive) { Task { await viewModel.stop(c) } }
            Button("Keep sending", role: .cancel) {}
        } message: { _ in
            Text("Texts already handed to the carrier are not recalled.")
        }
        .confirmationDialog(retrying.map { "Send \u{201C}\($0.subject)\u{201D} again to the \(mktPlural($0.retryable, "guest")) it failed to reach?" } ?? "",
                            isPresented: Binding(get: { retrying != nil }, set: { if !$0 { retrying = nil } }),
                            titleVisibility: .visible, presenting: retrying) { n in
            Button("Retry \(n.retryable)") {
                Task { await viewModel.retry(n) }
            }
            Button("Cancel", role: .cancel) {}
        } message: { _ in
            Text("Nobody it already reached is emailed twice.")
        }
        .sheet(isPresented: $showingRules) { rulesSheet }
    }

    // MARK: - Who's listening

    /// One headline row (readability round 10/8/26): who is listening and
    /// how that moved in 30 days, and what the last campaign did. The tap
    /// and came-back rates ride each campaign below; the twelve-week opt-in
    /// chart is the web's. A figure the server didn't send is "—", never 0.
    private var headlineRow: some View {
        let o = viewModel.overview
        return VStack(alignment: .leading, spacing: CavnarSpace.xs) {
            CavnarKicker("Who\u{2019}s listening")
            HStack(alignment: .firstTextBaseline, spacing: CavnarSpace.xs) {
                Text(o?.subscribers.map { "\($0)" } ?? "\u{2014}")
                    .cavnarText(.figureL, color: o?.subscribers == nil ? .cavnarInk3 : .cavnarInk)
                Text(o?.subscribers == 1 ? "subscriber" : "subscribers")
                    .cavnarText(.body)
                Spacer(minLength: CavnarSpace.xs)
                if let last30 = o?.last30 {
                    HomeMixedText.make(last30 > 0 ? "+\(last30) in 30 days" : "No new opt-ins in 30 days",
                                       role: .secondary,
                                       color: last30 > 0 ? .cavnarGreen : .cavnarInk2,
                                       numberColor: last30 > 0 ? .cavnarGreen : .cavnarInk2)
                }
            }
            .lineLimit(1)
            .minimumScaleFactor(0.85)
            if let o, let email = o.emailSubscribers, email > 0 {
                HomeMixedText.make("\(email) by email \u{00B7} +\(o.today ?? 0) today", role: .caption)
            }
            if let last = o?.lastCampaign {
                HomeMixedText.make("Last campaign"
                                   + (last.date.map { " \(CavnarDate.mdy($0))" } ?? "")
                                   + " \u{00B7} \(last.sent) " + (last.channel == "email" ? "emailed" : "texted"),
                                   role: .secondary)
            } else if o != nil {
                Text("Nothing sent yet").cavnarText(.secondary)
            }
        }
        .frame(maxWidth: .infinity, alignment: .leading)
        .accessibilityElement(children: .combine)
    }

    @ViewBuilder
    private var insights: some View {
        let list = viewModel.overview?.insights ?? []
        if !list.isEmpty {
            VStack(alignment: .leading, spacing: CavnarSpace.s) {
                ForEach(list) { it in
                    HStack(alignment: .firstTextBaseline, spacing: CavnarSpace.s) {
                        Text(it.figure)
                            .cavnarText(.figureM, color: it.tone == "good" ? .cavnarGreen : .cavnarInk)
                        VStack(alignment: .leading, spacing: 2) {
                            Text(it.text).cavnarText(.label)
                            if let basis = it.basis {
                                CavnarMixedText(basis, role: .caption)
                            }
                        }
                    }
                }
            }
            .frame(maxWidth: .infinity, alignment: .leading)
            .cavnarCard()
        }
    }

    // MARK: - Create

    /// The one "write" door for guest texts and emails on the phone — the
    /// Content tab writes posts only, the Text Club no longer has its own
    /// composer (readability round 10/8/26 #57).
    private var createCard: some View {
        // A win-back Cavnar AI suggests leads the card when there is one
        // (re-audit 10/8/26 L6) — it sat under the ideas, last.
        let suggesting = viewModel.winback != nil && !viewModel.winbackDismissed
        return VStack(alignment: .leading, spacing: CavnarSpace.s) {
            if let w = viewModel.winback {
                winbackRow(w)
                    .padding(.bottom, CavnarSpace.xs)
            }
            CavnarKicker("Create")
            Text("What should this campaign do?")
                .cavnarText(.headline)
            Text("One goal drafts the text, the email and the post at once. Nothing goes out until you send it.")
                .cavnarText(.secondary)
                .fixedSize(horizontal: false, vertical: true)
            Button {
                Haptic.light()
                onOpenStudio(StudioSeed())
            } label: {
                Label("New campaign", systemImage: "sparkles").frame(maxWidth: .infinity)
            }
            .modifier(CampaignCreateButtonStyle(primary: !suggesting))
            ForEach(CampaignStudioView.ideas, id: \.label) { idea in
                Button {
                    Haptic.light()
                    onOpenStudio(StudioSeed(prompt: idea.prompt, autoCreate: true))
                } label: {
                    HStack {
                        Text(idea.label).cavnarText(.label)
                        Spacer()
                        Image(systemName: "arrow.up.right").font(.cavnar(.caption))
                            .foregroundStyle(Color.cavnarInk3)
                    }
                    .padding(.horizontal, CavnarSpace.s)
                    .frame(minHeight: 44)
                    .background(RoundedRectangle(cornerRadius: CavnarRadius.control).fill(Color.white.opacity(0.04)))
                    .contentShape(Rectangle())
                }
                .buttonStyle(.plain)
            }
        }
        .cavnarCard(.ai)
    }

    private func winbackRow(_ w: GuestWinback.Draft) -> some View {
        VStack(alignment: .leading, spacing: CavnarSpace.xs) {
            CavnarKicker("Cavnar AI suggests")
            if viewModel.winbackDismissed {
                Text("Noted \u{2014} no win-back suggestion for this group.")
                    .cavnarText(.secondary)
            } else {
                HomeMixedText.make("Bring back \(mktPlural(w.segmentSize ?? 0, "guest")) \((w.segmentLabel ?? "").lowercased())",
                                   role: .label)
                    .fixedSize(horizontal: false, vertical: true)
                HStack(spacing: 16) {
                    // The card's one primary while it is suggested.
                    Button {
                        Haptic.light()
                        onOpenStudio(StudioSeed(winback: w))
                    } label: { Text("Use this") }
                    .buttonStyle(CavnarPrimaryButtonStyle())
                    Button {
                        Haptic.light()
                        askingWinbackReason = true
                    } label: {
                        Text(RecAnswer.notForUs.label)
                            .font(.cavnarBody(CavnarType.secondary, weight: 700))
                            .foregroundStyle(Color.cavnarInk2)
                            .frame(minHeight: 44)
                            .contentShape(Rectangle())
                    }
                    .buttonStyle(.plain)
                    .recReasonDialog(isPresented: $askingWinbackReason, skipLabel: "Skip",
                                     onSkip: { Task { await viewModel.dismissWinback(reasonCode: nil) } }) { reason in
                        Task { await viewModel.dismissWinback(reasonCode: reason.code) }
                    }
                }
            }
        }
    }

    // MARK: - What went out

    @ViewBuilder
    private var historySection: some View {
        let items = Array(viewModel.history.prefix(9))
        VStack(alignment: .leading, spacing: CavnarSpace.s) {
            VStack(alignment: .leading, spacing: CavnarSpace.xxs) {
                CavnarKicker("What went out")
                Text("Campaigns sent").cavnarText(.headline)
            }
            if let line = viewModel.monthLine {
                CavnarMixedText(line, role: .caption)
            }
            if let notice = viewModel.notice {
                CampaignCheckLine(ok: true, text: notice)
            }
            if let error = viewModel.actionError {
                Text(error).cavnarText(.secondary, color: .cavnarRedText)
                    .fixedSize(horizontal: false, vertical: true)
            }
            if items.isEmpty {
                Text(viewModel.isLoading ? "" : "Your first campaign lands here, with what it did.")
                    .cavnarText(.body)
            } else {
                List {
                    ForEach(items) { item in
                        historyRow(item)
                            .cavnarReportsRowHeight(item.id, into: $rowHeights)
                            .listRowBackground(Color.clear)
                            .listRowInsets(EdgeInsets(top: 6, leading: 0, bottom: 6, trailing: 0))
                            .listRowSeparator(.hidden)
                            .swipeActions(edge: .trailing, allowsFullSwipe: false) { swipeActions(item) }
                            .contextMenu { contextActions(item) }
                    }
                }
                .listStyle(.plain)
                .scrollContentBackground(.hidden)
                .scrollDisabled(true)
                .frame(height: CavnarFittedList.height(ids: items.map(\.id), measured: rowHeights, verticalInsets: 12))
            }
        }
    }

    @ViewBuilder
    private func swipeActions(_ item: CampaignHistoryItem) -> some View {
        switch item {
        case .text(let c):
            if canPublish, c.isOpen {
                Button(role: .destructive) { stopping = c } label: { Label("Stop sending", systemImage: "stop.circle") }
            }
        case .email(let n):
            if canPublish, n.retryable > 0 {
                Button { retrying = n } label: { Label("Retry \(n.retryable)", systemImage: "arrow.clockwise") }
                    .tint(Color.cavnarEmber)
            }
        }
    }

    @ViewBuilder
    private func contextActions(_ item: CampaignHistoryItem) -> some View {
        switch item {
        case .text(let c):
            Button { onOpenStudio(StudioSeed(reuseText: c)) } label: { Label("Use again", systemImage: "arrow.uturn.right") }
            Button { onOpenStudio(StudioSeed(reuseText: c, improve: true)) } label: {
                Label("Improve with Cavnar AI", systemImage: "sparkles")
            }
            if canPublish, c.isOpen {
                Button(role: .destructive) { stopping = c } label: { Label("Stop sending", systemImage: "stop.circle") }
            }
        case .email(let n):
            Button { onOpenStudio(StudioSeed(reuseEmail: n)) } label: { Label("Use again", systemImage: "arrow.uturn.right") }
            Button { onOpenStudio(StudioSeed(reuseEmail: n, improve: true)) } label: {
                Label("Improve with Cavnar AI", systemImage: "sparkles")
            }
            if canPublish, n.retryable > 0 {
                Button { retrying = n } label: { Label("Retry \(n.retryable) failed", systemImage: "arrow.clockwise") }
            }
        }
    }

    @ViewBuilder
    private func historyRow(_ item: CampaignHistoryItem) -> some View {
        switch item {
        case .text(let c): textRow(c)
        case .email(let n): emailRow(n)
        }
    }

    private func statusTint(_ c: GuestCampaign) -> Color {
        switch c.status {
        case "waiting", "sending": return c.pending > 0 ? .cavnarAmber : .cavnarGreen
        case "cancelled": return .cavnarInk3
        default: return .cavnarGreen
        }
    }

    /// The channel as a capsule tag — TEXT or EMAIL.
    private func channelTag(_ label: String) -> some View {
        Text(label)
            .cavnarText(.tag, color: .cavnarEmber2)
            .padding(.horizontal, 7)
            .padding(.vertical, 2)
            .background(Capsule().fill(Color.cavnarEmber.opacity(0.14)))
    }

    /// A text campaign: when, to whom, the words (two lines), what it did,
    /// and Stop sending while it is still going out.
    private func textRow(_ c: GuestCampaign) -> some View {
        let busy = viewModel.busyIDs.contains("t\(c.id)")
        return VStack(alignment: .leading, spacing: CavnarSpace.xs) {
            HStack(spacing: 8) {
                channelTag("Text")
                HomeMixedText.make(c.whenLabel, role: .caption)
                if c.id == viewModel.bestTextID {
                    AccountChip(text: "Best", tint: .cavnarGreen)
                }
                Spacer()
                AccountChip(text: c.statusLabel, tint: statusTint(c))
            }
            Text(c.message)
                .cavnarText(.body)
                .lineLimit(2)
                .fixedSize(horizontal: false, vertical: true)
            if let label = c.segmentLabel {
                Text(label).cavnarText(.caption)
            }
            metricRow([("Texted", "\(c.sentCount)", Color.cavnarInk),
                       c.failedCount > 0 ? ("Failed", "\(c.failedCount)", Color.cavnarRedText) : nil,
                       ("Tapped", c.linkToken != nil ? "\(c.clicks)" : "no link", Color.cavnarEmber2),
                       // Still inside its window: "measuring", never an
                       // ellipsis that reads as loading (L18).
                       ("Came back", c.visitsMatched.map { "\($0)" } ?? (c.attributionThrough != nil ? "0" : "measuring"),
                        Color.cavnarGreen)])
            reuseRow(again: StudioSeed(reuseText: c), improve: StudioSeed(reuseText: c, improve: true))
            if canPublish, c.isOpen {
                Button(role: .destructive) {
                    Haptic.light()
                    stopping = c
                } label: {
                    Group {
                        if busy { CavnarShimmerText(text: "Stopping\u{2026}", color: .cavnarRedText) }
                        else {
                            Text("Stop sending").font(.cavnarBody(CavnarType.secondary, weight: 700))
                                .foregroundStyle(Color.cavnarRedText)
                        }
                    }
                    .frame(minHeight: 44)
                    .contentShape(Rectangle())
                }
                .buttonStyle(.plain)
                .disabled(busy)
            }
        }
        .padding(CavnarSpace.m)
        .frame(maxWidth: .infinity, alignment: .leading)
        .background(Color.cavnarPaper2)
        .clipShape(RoundedRectangle(cornerRadius: CavnarRadius.control))
    }

    /// A newsletter: when, the subject, what it did ("opens recorded" — a
    /// floor that includes Apple Mail's auto-opens), and Retry for failures.
    private func emailRow(_ n: GuestNewsletter) -> some View {
        let busy = viewModel.busyIDs.contains("e\(n.id)")
        return VStack(alignment: .leading, spacing: CavnarSpace.xs) {
            HStack(spacing: 8) {
                channelTag("Email")
                HomeMixedText.make(n.whenLabel, role: .caption)
                Spacer()
                if let label = n.segmentLabel { AccountChip(text: label, muted: true) }
            }
            Text(n.subject.isEmpty ? "Newsletter" : n.subject)
                .cavnarText(.label)
                .lineLimit(2)
            metricRow([("Emailed", "\(n.sent)", Color.cavnarInk),
                       n.failed > 0 ? ("Failed", "\(n.failed)", Color.cavnarRedText) : nil,
                       n.pending > 0 ? ("Queued", "\(n.pending)", Color.cavnarInk2) : nil,
                       ("Opens recorded", n.opened.map { "\($0)" } ?? "not tracked", Color.cavnarEmber2),
                       ("Clicks recorded", n.clicked.map { "\($0)" } ?? "not tracked", Color.cavnarGreen)])
            reuseRow(again: StudioSeed(reuseEmail: n), improve: StudioSeed(reuseEmail: n, improve: true))
            if n.opened != nil || n.resultsAsOf != nil {
                Text((n.opened != nil ? "Opens include Apple Mail auto-opens." : "")
                     + (n.resultsAsOf.map { (n.opened != nil ? " " : "") + "Figures as of \(CampaignDates.label($0))." } ?? ""))
                    .cavnarText(.caption)
                    .fixedSize(horizontal: false, vertical: true)
            }
            if canPublish, n.retryable > 0 {
                HStack(spacing: 16) {
                    Button {
                        Haptic.light()
                        retrying = n
                    } label: {
                        Text("Retry \(n.retryable) failed").font(.cavnarBody(CavnarType.secondary, weight: 700))
                            .foregroundStyle(Color.cavnarEmber2)
                            .frame(minHeight: 44)
                            .contentShape(Rectangle())
                    }
                    .buttonStyle(.plain)
                    if busy { CavnarShimmerText(text: "Sending\u{2026}", color: .cavnarEmber2) }
                }
                .disabled(busy)
            }
        }
        .padding(CavnarSpace.m)
        .frame(maxWidth: .infinity, alignment: .leading)
        .background(Color.cavnarPaper2)
        .clipShape(RoundedRectangle(cornerRadius: CavnarRadius.control))
    }

    /// "Use again" and "Improve with Cavnar AI" on the row itself (re-audit
    /// 10/8/26 L19) — they lived only in the long-press menu.
    private func reuseRow(again: StudioSeed, improve: StudioSeed) -> some View {
        HStack(spacing: CavnarSpace.m) {
            Button {
                Haptic.light()
                onOpenStudio(again)
            } label: {
                Label("Use again", systemImage: "arrow.uturn.right")
                    .font(.cavnarBody(CavnarType.secondary, weight: 700))
                    .foregroundStyle(Color.cavnarEmber2)
                    .frame(minHeight: 44)
                    .contentShape(Rectangle())
            }
            .buttonStyle(.plain)
            Button {
                Haptic.light()
                onOpenStudio(improve)
            } label: {
                Label("Improve with Cavnar AI", systemImage: "sparkles")
                    .font(.cavnarBody(CavnarType.secondary, weight: 700))
                    .foregroundStyle(Color.cavnarEmber2)
                    .lineLimit(1)
                    .minimumScaleFactor(0.85)
                    .frame(minHeight: 44)
                    .contentShape(Rectangle())
            }
            .buttonStyle(.plain)
            Spacer(minLength: 0)
        }
    }

    private func metricRow(_ maybe: [(String, String, Color)?]) -> some View {
        let items = maybe.compactMap { $0 }
        return HStack(alignment: .top, spacing: CavnarSpace.m) {
            ForEach(Array(items.enumerated()), id: \.offset) { _, item in
                VStack(alignment: .leading, spacing: 2) {
                    if Int(item.1) != nil {
                        Text(item.1).cavnarText(.figureS, color: item.2)
                    } else {
                        Text(item.1).cavnarText(.caption, color: .cavnarInk2)
                    }
                    Text(item.0).cavnarText(.caption)
                }
            }
        }
        .lineLimit(1)
        .minimumScaleFactor(0.85)
    }

    // MARK: - Rules, consent and the Text Club

    /// Two rows where the rules card stood: the rules every campaign
    /// follows (with the invite switch) in a sheet, and the Text Club —
    /// contacts, the QR code and the join link (readability round #57).
    private var rulesAndClubRows: some View {
        VStack(spacing: 0) {
            navRow(icon: "checkmark.shield", title: "Rules & consent",
                   subtitle: viewModel.ledger.map { "\($0.textable) opted in \u{00B7} sending hours, spacing, opt-outs" }
                       ?? "Sending hours, spacing, opt-outs and invites") {
                showingRules = true
            }
            Rectangle().fill(Color.cavnarPaper3).frame(height: 1)
            navRow(icon: "person.2", title: "Contacts, QR code and join link",
                   subtitle: "The Guest Text Club") {
                onOpenTextClub()
            }
        }
        .cavnarCard()
    }

    private func navRow(icon: String, title: String, subtitle: String, action: @escaping () -> Void) -> some View {
        Button {
            Haptic.light()
            action()
        } label: {
            HStack(spacing: CavnarSpace.s) {
                Image(systemName: icon).frame(width: 22).foregroundStyle(Color.cavnarEmber2)
                VStack(alignment: .leading, spacing: 2) {
                    Text(title).cavnarText(.label)
                    CavnarMixedText(subtitle, role: .caption)
                }
                Spacer()
                Image(systemName: "chevron.right").font(.cavnar(.caption)).foregroundStyle(Color.cavnarInk3)
            }
            .frame(minHeight: 52)
            .contentShape(Rectangle())
        }
        .buttonStyle(.plain)
    }

    /// The rules every campaign follows, said rather than assumed, and the
    /// review-link invite switch with its disclosure.
    private var rulesSheet: some View {
        let o = viewModel.overview
        return NavigationStack {
            ScrollView {
                VStack(alignment: .leading, spacing: CavnarSpace.m) {
                    // The invite switch is the phone's; the rules every
                    // campaign follows — sending hours, spacing, opt-outs,
                    // consent counts, the mailing address — are read on the
                    // web (re-audit 10/8/26 W10). Nothing about them changed.
                    Text("Review-link invites").cavnarText(.headline)
                    invitesRow
                    CavnarWebLinkRow(title: "The rules every campaign follows",
                                     subtitle: SMSWindow.range(o?.window).map { "Texts go out \($0), your time; STOP and HELP are handled for you" }
                                         ?? "Sending hours, spacing, opt-outs and consent",
                                     path: "marketing/guests", actionLabel: "Open on the web")
                }
                .padding(CavnarSpace.gutter)
            }
            .accountSheetChrome("Rules & consent")
            .sheet(isPresented: $showingDisclosure) { disclosureSheet }
        }
        .presentationDetents([.large])
    }

    private func settingRow(_ icon: String, _ title: String, _ value: String, always: Bool = false) -> some View {
        HStack(alignment: .firstTextBaseline, spacing: 10) {
            Image(systemName: icon).frame(width: 22).foregroundStyle(Color.cavnarEmber2)
            VStack(alignment: .leading, spacing: 2) {
                Text(title).cavnarText(.label)
                CavnarMixedText(value, role: .secondary)
            }
            Spacer(minLength: 6)
            if always {
                Text("Always on").font(.cavnarBody(CavnarType.caption, weight: 700)).foregroundStyle(Color.cavnarGreen)
            }
        }
        .accessibilityElement(children: .combine)
    }

    /// The Toast review-link invites: off until the owner turns them on;
    /// turning on is the acknowledgement of the sentence shown first.
    @ViewBuilder
    private var invitesRow: some View {
        if let inv = viewModel.invites {
            VStack(alignment: .leading, spacing: 6) {
                HStack(alignment: .center, spacing: 10) {
                    Image(systemName: "phone.bubble").frame(width: 22).foregroundStyle(Color.cavnarEmber2)
                    VStack(alignment: .leading, spacing: 2) {
                        Text("Review-link invites").cavnarText(.label)
                        Text(inv.enabled ? "On \u{2014} one invite each afternoon to guests from your last service" : "Off")
                            .cavnarText(.secondary)
                            .fixedSize(horizontal: false, vertical: true)
                    }
                    Spacer()
                    Toggle("", isOn: Binding(
                        get: { inv.enabled },
                        set: { on in
                            if on { showingDisclosure = true } else { Task { await viewModel.setInvites(false) } }
                        }))
                        .labelsHidden()
                        .tint(Color.cavnarEmber)
                        .disabled(!(inv.canChange ?? canChangeInvites) || viewModel.invitesBusy)
                        .accessibilityLabel("Text guests from your last service one review-link invite")
                }
                if !(inv.canChange ?? canChangeInvites) {
                    Text("Only the account owner can turn invite texts on or off.")
                        .cavnarText(.caption)
                }
                if let error = viewModel.invitesError {
                    Text(error).cavnarText(.caption, color: .cavnarRedText)
                }
            }
        }
    }

    private var disclosureSheet: some View {
        NavigationStack {
            ScrollView {
                VStack(alignment: .leading, spacing: 16) {
                    CavnarKicker("Review-link invites")
                    Text("Before you turn this on")
                        .cavnarText(.headline)
                    Text(viewModel.invites?.disclosure ?? "")
                        .cavnarText(.lead)
                        .fixedSize(horizontal: false, vertical: true)
                    Text("Turning it on records that you agreed to this, with who and when.")
                        .cavnarText(.secondary)
                        .fixedSize(horizontal: false, vertical: true)
                    Button {
                        Haptic.light()
                        showingDisclosure = false
                        Task { await viewModel.setInvites(true) }
                    } label: {
                        Text("I agree \u{2014} turn invites on").frame(maxWidth: .infinity)
                    }
                    .buttonStyle(CavnarPrimaryButtonStyle())
                    Button {
                        showingDisclosure = false
                    } label: {
                        Text("Not now").frame(maxWidth: .infinity)
                    }
                    .buttonStyle(CavnarSecondaryButtonStyle())
                }
                .padding(20)
            }
            .accountSheetChrome("Invites")
        }
        .presentationDetents([.medium, .large])
    }
}
