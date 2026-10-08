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
/// and emails, with Stop sending, Retry and Send to new) and the rules
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
    /// carrier" — accepted, never "delivered" (CS-13).
    var monthLine: String? {
        guard let o = overview else { return nil }
        var bits = ["\(o.textsThisMonth ?? 0) texted this month"]
        let month = Self.monthKey(Date())
        let mailed = newsletters.filter { String(($0.createdAt ?? "").prefix(7)) == month }.reduce(0) { $0 + $1.sent }
        if mailed > 0 { bits.append("\(mailed) emailed") }
        if let f = o.textsFailedThisMonth, f > 0 { bits.append("\(mktPlural(f, "text")) failed") }
        if let a = o.accepted {
            bits.append((a == a.rounded() ? "\(Int(a))" : String(format: "%.1f", a)) + "% accepted by carrier")
        }
        return bits.joined(separator: " \u{00B7} ")
    }

    private static func monthKey(_ date: Date) -> String {
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

    /// Retry the failures a retry can reach, or send to the new
    /// subscribers. Confirmed by the caller first.
    func followUp(_ newsletter: GuestNewsletter, retry: Bool) async {
        let key = "e\(newsletter.id)"
        busyIDs.insert(key)
        defer { busyIDs.remove(key) }
        actionError = nil
        let r = await CampaignMail.followUp(client: client, newsletterId: newsletter.id, retry: retry)
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

struct CampaignsTabSection: View {
    let viewModel: CampaignsTabViewModel
    var isOwner: Bool
    var onOpenStudio: (StudioSeed) -> Void
    var onOpenTextClub: () -> Void

    @State private var rowHeights: [String: CGFloat] = [:]
    @State private var stopping: GuestCampaign?
    @State private var following: (newsletter: GuestNewsletter, retry: Bool)?
    @State private var askingWinbackReason = false
    @State private var showingDisclosure = false

    var body: some View {
        VStack(alignment: .leading, spacing: 18) {
            if let error = viewModel.loadError, viewModel.overview == nil {
                Text(error).font(.cavnarBody(CavnarType.body)).foregroundStyle(Color.cavnarRed)
            }
            if viewModel.overview == nil && viewModel.isLoading {
                CavnarSkeletonLines(widths: [1.0, 0.8, 0.9, 0.6]).padding(.vertical, 12)
            }
            kpiStrip
            growthCard
            insights
            createCard
            historySection
            settingsCard
        }
        .confirmationDialog(stopping.map { "Stop sending? \(mktPlural($0.pending, "text")) won\u{2019}t go out." } ?? "",
                            isPresented: Binding(get: { stopping != nil }, set: { if !$0 { stopping = nil } }),
                            titleVisibility: .visible, presenting: stopping) { c in
            Button("Stop sending", role: .destructive) { Task { await viewModel.stop(c) } }
            Button("Keep sending", role: .cancel) {}
        } message: { _ in
            Text("Texts already handed to the carrier are not recalled.")
        }
        .confirmationDialog(followTitle,
                            isPresented: Binding(get: { following != nil }, set: { if !$0 { following = nil } }),
                            titleVisibility: .visible) {
            if let f = following {
                Button(f.retry ? "Retry \(f.newsletter.retryable)" : "Send it") {
                    Task { await viewModel.followUp(f.newsletter, retry: f.retry) }
                }
            }
            Button("Cancel", role: .cancel) {}
        } message: {
            Text("Nobody it already reached is emailed twice.")
        }
        .sheet(isPresented: $showingDisclosure) { disclosureSheet }
    }

    private var followTitle: String {
        guard let f = following else { return "" }
        return f.retry ? "Send \u{201C}\(f.newsletter.subject)\u{201D} again to the \(mktPlural(f.newsletter.retryable, "guest")) it failed to reach?"
            : "Send \u{201C}\(f.newsletter.subject)\u{201D} to the subscribers it hasn\u{2019}t reached?"
    }

    // MARK: - Who's listening

    private var kpiStrip: some View {
        let o = viewModel.overview
        let rateMin = o?.rateMin ?? 2
        return LazyVGrid(columns: [GridItem(.flexible(), spacing: 10), GridItem(.flexible(), spacing: 10)], spacing: 10) {
            kpi("Subscribers", value: o?.subscribers.map { "\($0)" },
                sub: o.map { "+\($0.today ?? 0) today" + (($0.emailSubscribers ?? 0) > 0 ? " \u{00B7} \($0.emailSubscribers ?? 0) by email" : "") })
            kpi("Growth", value: o?.last30.map { $0 > 0 ? "+\($0)" : "0" }, sub: "new opt-ins, 30 days")
            kpi("Last campaign", value: o?.lastCampaign?.date.map { CavnarDate.mdy($0) },
                sub: o?.lastCampaign.map { "\($0.sent) " + ($0.channel == "email" ? "emailed" : "texted") } ?? "nothing sent yet")
            kpi("Tap rate", value: o?.tapRate?.label,
                sub: o?.tapRate.map { "\(mktPlural($0.campaigns, "campaign")) with a link" }
                    ?? "after \(rateMin) campaigns with a link")
            kpi("Came back", value: o?.backRate?.label,
                sub: o?.backRate.map { "\(mktPlural($0.campaigns, "campaign")), matched in your POS" } ?? "within 14 days")
        }
    }

    private func kpi(_ label: String, value: String?, sub: String?) -> some View {
        VStack(alignment: .leading, spacing: 4) {
            Text(label.uppercased())
                .font(.cavnarBody(CavnarType.kicker, weight: 700))
                .tracking(1.1)
                .foregroundStyle(Color.cavnarEmber2)
            Text(value ?? "\u{2014}")
                .font(.cavnarNumber(CavnarType.tileNumber, weight: 700))
                .foregroundStyle(value == nil ? Color.cavnarInk3 : Color.cavnarInk)
                .lineLimit(1)
                .minimumScaleFactor(0.7)
            if let sub {
                HomeMixedText.make(sub, size: CavnarType.caption, weight: 500, color: .cavnarInk3)
                    .lineLimit(2)
                    .fixedSize(horizontal: false, vertical: true)
            }
        }
        .frame(maxWidth: .infinity, minHeight: 92, alignment: .topLeading)
        .padding(12)
        .background(Color.cavnarPaper2)
        .overlay(RoundedRectangle(cornerRadius: CavnarRadius.card).strokeBorder(Color.cavnarPaper3, lineWidth: 1))
        .clipShape(RoundedRectangle(cornerRadius: CavnarRadius.card))
        .accessibilityElement(children: .combine)
    }

    /// Opt-ins per week, the last 12 weeks: ember bars, this week lit. A
    /// week with no opt-ins is a measured 0; no data at all is the frame
    /// with a sentence, never a row of zeros.
    @ViewBuilder
    private var growthCard: some View {
        let weeks = viewModel.overview?.weekly ?? []
        VStack(alignment: .leading, spacing: 10) {
            HStack(alignment: .firstTextBaseline) {
                CampaignKicker(text: "Opt-ins per week")
                Spacer()
                if let last30 = viewModel.overview?.last30 {
                    HomeMixedText.make("+\(last30) in 30 days", size: CavnarType.caption, weight: 700, color: .cavnarGreen)
                }
            }
            if weeks.isEmpty {
                Text(viewModel.overview == nil ? "\u{2014}" : "Opt-ins show here week by week once guests start joining.")
                    .font(.cavnarBody(CavnarType.secondary))
                    .foregroundStyle(Color.cavnarInk3)
                    .frame(maxWidth: .infinity, minHeight: 120)
            } else {
                Chart {
                    ForEach(Array(weeks.enumerated()), id: \.element.id) { i, w in
                        BarMark(x: .value("Week", w.shortLabel), y: .value("Joined", w.joined))
                            .foregroundStyle(LinearGradient(
                                colors: [Color.cavnarEmber.opacity(i == weeks.count - 1 ? 1 : 0.85),
                                         Color.cavnarEmber2.opacity(i == weeks.count - 1 ? 0.9 : 0.35)],
                                startPoint: .top, endPoint: .bottom))
                            .cornerRadius(4)
                            .annotation(position: .top, spacing: 2) {
                                if w.joined > 0 {
                                    Text("\(w.joined)").font(.cavnarNumber(10, weight: 700)).foregroundStyle(Color.cavnarInk3)
                                }
                            }
                    }
                }
                .chartYAxis(.hidden)
                .chartXAxis {
                    AxisMarks(values: [weeks.first?.shortLabel ?? "", weeks.last?.shortLabel ?? ""]) { _ in
                        AxisValueLabel().font(.cavnarNumber(10.5)).foregroundStyle(Color.cavnarInk3)
                    }
                }
                .frame(height: 140)
                .shadow(color: Color.cavnarEmber.opacity(0.25), radius: 8, y: 2)
                .accessibilityLabel("Opt-ins per week, last 12 weeks")
                if let first = weeks.first?.weekStart, let last = weeks.last?.weekStart {
                    Text("Weeks of \(CavnarDate.mdy(first)) to \(CavnarDate.mdy(last))")
                        .font(.cavnarNumber(11))
                        .foregroundStyle(Color.cavnarInk3)
                }
            }
        }
        .cavnarCard()
    }

    @ViewBuilder
    private var insights: some View {
        let list = viewModel.overview?.insights ?? []
        if !list.isEmpty {
            VStack(alignment: .leading, spacing: 10) {
                ForEach(list) { it in
                    HStack(alignment: .firstTextBaseline, spacing: 10) {
                        Text(it.figure)
                            .font(.cavnarNumber(22, weight: 700))
                            .foregroundStyle(it.tone == "good" ? Color.cavnarGreen : Color.cavnarInk)
                        VStack(alignment: .leading, spacing: 2) {
                            Text(it.text).font(.cavnarBody(CavnarType.body, weight: 600)).foregroundStyle(Color.cavnarInk)
                            if let basis = it.basis {
                                HomeMixedText.make(basis, size: CavnarType.caption, weight: 500, color: .cavnarInk3)
                                    .fixedSize(horizontal: false, vertical: true)
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

    private var createCard: some View {
        VStack(alignment: .leading, spacing: 12) {
            CampaignKicker(text: "Create")
            Text("What should this campaign do?")
                .font(.cavnarHeadline(CavnarType.section))
                .foregroundStyle(Color.cavnarInk)
            Text("One goal drafts the text, the email and the post at once. Nothing goes out until you send it.")
                .font(.cavnarBody(CavnarType.secondary))
                .foregroundStyle(Color.cavnarInk3)
                .fixedSize(horizontal: false, vertical: true)
            Button {
                Haptic.light()
                onOpenStudio(StudioSeed())
            } label: {
                Label("New campaign", systemImage: "sparkles").frame(maxWidth: .infinity)
            }
            .buttonStyle(CavnarPrimaryButtonStyle())
            ForEach(CampaignStudioView.ideas, id: \.label) { idea in
                Button {
                    Haptic.light()
                    onOpenStudio(StudioSeed(prompt: idea.prompt, autoCreate: true))
                } label: {
                    HStack {
                        Text(idea.label).font(.cavnarBody(14.5, weight: 600)).foregroundStyle(Color.cavnarInk)
                        Spacer()
                        Image(systemName: "arrow.up.right").font(.system(size: 11, weight: .semibold))
                            .foregroundStyle(Color.cavnarInk3)
                    }
                    .padding(.horizontal, 12)
                    .frame(minHeight: 44)
                    .background(RoundedRectangle(cornerRadius: CavnarRadius.control).fill(Color.white.opacity(0.04)))
                    .contentShape(Rectangle())
                }
                .buttonStyle(.plain)
            }
            if let w = viewModel.winback {
                winbackRow(w)
            }
        }
        .cavnarCard(.ai)
    }

    private func winbackRow(_ w: GuestWinback.Draft) -> some View {
        VStack(alignment: .leading, spacing: 8) {
            CampaignKicker(text: "Cavnar AI suggests")
            if viewModel.winbackDismissed {
                Text("Noted \u{2014} no win-back suggestion for this group.")
                    .font(.cavnarBody(CavnarType.secondary))
                    .foregroundStyle(Color.cavnarInk3)
            } else {
                HomeMixedText.make("Bring back \(mktPlural(w.segmentSize ?? 0, "guest")) \((w.segmentLabel ?? "").lowercased())",
                                   size: CavnarType.body, weight: 700, color: .cavnarInk)
                    .fixedSize(horizontal: false, vertical: true)
                HStack(spacing: 16) {
                    Button {
                        Haptic.light()
                        onOpenStudio(StudioSeed(winback: w))
                    } label: { Text("Use this") }
                    .buttonStyle(CavnarSoftButtonStyle())
                    Button {
                        Haptic.light()
                        askingWinbackReason = true
                    } label: {
                        Text(RecAnswer.notForUs.label)
                            .font(.cavnarBody(CavnarType.secondary, weight: 600))
                            .foregroundStyle(Color.cavnarInk3)
                            .frame(minHeight: 44)
                    }
                    .buttonStyle(.plain)
                    .recReasonDialog(isPresented: $askingWinbackReason, skipLabel: "Skip",
                                     onSkip: { Task { await viewModel.dismissWinback(reasonCode: nil) } }) { reason in
                        Task { await viewModel.dismissWinback(reasonCode: reason.code) }
                    }
                }
            }
        }
        .padding(.top, 6)
    }

    // MARK: - What went out

    @ViewBuilder
    private var historySection: some View {
        let items = Array(viewModel.history.prefix(9))
        VStack(alignment: .leading, spacing: 10) {
            HomeSectionHeader(kicker: "Performance", title: "Campaigns sent")
            if let line = viewModel.monthLine {
                HomeMixedText.make(line, size: CavnarType.caption, weight: 600, color: .cavnarInk3)
                    .fixedSize(horizontal: false, vertical: true)
            }
            if let notice = viewModel.notice {
                CampaignCheckLine(ok: true, text: notice)
            }
            if let error = viewModel.actionError {
                Text(error).font(.cavnarBody(CavnarType.secondary)).foregroundStyle(Color.cavnarRed)
                    .fixedSize(horizontal: false, vertical: true)
            }
            if items.isEmpty {
                Text(viewModel.isLoading ? "" : "Your first campaign lands here, with what it did.")
                    .font(.cavnarBody(CavnarType.body))
                    .foregroundStyle(Color.cavnarInk3)
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
            if c.isOpen {
                Button(role: .destructive) { stopping = c } label: { Label("Stop sending", systemImage: "stop.circle") }
            }
        case .email(let n):
            if n.retryable > 0 {
                Button { following = (n, true) } label: { Label("Retry \(n.retryable)", systemImage: "arrow.clockwise") }
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
            if c.isOpen {
                Button(role: .destructive) { stopping = c } label: { Label("Stop sending", systemImage: "stop.circle") }
            }
        case .email(let n):
            Button { onOpenStudio(StudioSeed(reuseEmail: n)) } label: { Label("Use again", systemImage: "arrow.uturn.right") }
            Button { onOpenStudio(StudioSeed(reuseEmail: n, improve: true)) } label: {
                Label("Improve with Cavnar AI", systemImage: "sparkles")
            }
            if n.retryable > 0 {
                Button { following = (n, true) } label: { Label("Retry \(n.retryable) failed", systemImage: "arrow.clockwise") }
            }
            Button { following = (n, false) } label: { Label("Send to new subscribers", systemImage: "person.badge.plus") }
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

    private func textRow(_ c: GuestCampaign) -> some View {
        let busy = viewModel.busyIDs.contains("t\(c.id)")
        return VStack(alignment: .leading, spacing: 8) {
            HStack(spacing: 8) {
                Text("TEXT").font(.cavnarBody(11, weight: 800)).tracking(1).foregroundStyle(Color.cavnarEmber2)
                Text(c.whenLabel).font(.cavnarNumber(13, weight: 600)).foregroundStyle(Color.cavnarInk3)
                if c.id == viewModel.bestTextID {
                    AccountChip(text: "Best", tint: .cavnarGreen)
                }
                Spacer()
                AccountChip(text: c.statusLabel, tint: statusTint(c))
            }
            if let label = c.segmentLabel {
                Text(label).font(.cavnarBody(CavnarType.caption, weight: 600)).foregroundStyle(Color.cavnarInk3)
            }
            Text(c.message)
                .font(.cavnarBody(CavnarType.body))
                .foregroundStyle(Color.cavnarInk)
                .lineLimit(3)
                .fixedSize(horizontal: false, vertical: true)
            metricRow([("Texted", "\(c.sentCount)", Color.cavnarInk2),
                       c.failedCount > 0 ? ("Failed", "\(c.failedCount)", Color.cavnarRed) : nil,
                       ("Tapped", c.linkToken != nil ? "\(c.clicks)" : "no link", Color.cavnarEmber2),
                       ("Came back", c.visitsMatched.map { "\($0)" } ?? (c.attributionThrough != nil ? "0" : "\u{2026}"),
                        Color.cavnarGreen)])
            if c.isOpen {
                Button(role: .destructive) {
                    Haptic.light()
                    stopping = c
                } label: {
                    if busy { CavnarShimmerText(text: "Stopping\u{2026}", color: .cavnarRed) }
                    else { Text("Stop sending").font(.cavnarBody(CavnarType.secondary, weight: 700)).foregroundStyle(Color.cavnarRed) }
                }
                .buttonStyle(.plain)
                .frame(minHeight: 32)
                .disabled(busy)
            }
        }
        .padding(14)
        .frame(maxWidth: .infinity, alignment: .leading)
        .background(Color.cavnarPaper2)
        .clipShape(RoundedRectangle(cornerRadius: CavnarRadius.control))
    }

    private func emailRow(_ n: GuestNewsletter) -> some View {
        let busy = viewModel.busyIDs.contains("e\(n.id)")
        return VStack(alignment: .leading, spacing: 8) {
            HStack(spacing: 8) {
                Text("EMAIL").font(.cavnarBody(11, weight: 800)).tracking(1).foregroundStyle(Color.cavnarEmber2)
                Text(n.whenLabel).font(.cavnarNumber(13, weight: 600)).foregroundStyle(Color.cavnarInk3)
                Spacer()
                if let label = n.segmentLabel { AccountChip(text: label, muted: true) }
            }
            Text(n.subject.isEmpty ? "Newsletter" : n.subject)
                .font(.cavnarBody(CavnarType.body, weight: 700))
                .foregroundStyle(Color.cavnarInk)
                .lineLimit(2)
            metricRow([("Emailed", "\(n.sent)", Color.cavnarInk2),
                       n.failed > 0 ? ("Failed", "\(n.failed)", Color.cavnarRed) : nil,
                       n.pending > 0 ? ("Queued", "\(n.pending)", Color.cavnarInk2) : nil,
                       ("Opens recorded", n.opened.map { "\($0)" } ?? "not tracked", Color.cavnarEmber2),
                       ("Clicks recorded", n.clicked.map { "\($0)" } ?? "not tracked", Color.cavnarGreen)])
            if n.opened != nil {
                Text("Opens include Apple Mail auto-opens.")
                    .font(.cavnarBody(CavnarType.caption))
                    .foregroundStyle(Color.cavnarInk3)
            }
            if let asOf = n.resultsAsOf {
                Text("Figures as of \(CampaignDates.label(asOf)).")
                    .font(.cavnarBody(CavnarType.caption))
                    .foregroundStyle(Color.cavnarInk3)
            }
            HStack(spacing: 16) {
                if n.retryable > 0 {
                    Button {
                        Haptic.light()
                        following = (n, true)
                    } label: {
                        Text("Retry \(n.retryable) failed").font(.cavnarBody(CavnarType.secondary, weight: 700))
                            .foregroundStyle(Color.cavnarEmber2)
                    }
                    .buttonStyle(.plain)
                    .frame(minHeight: 32)
                }
                Button {
                    Haptic.light()
                    following = (n, false)
                } label: {
                    Text("Send to new subscribers").font(.cavnarBody(CavnarType.secondary, weight: 700))
                        .foregroundStyle(Color.cavnarEmber2)
                }
                .buttonStyle(.plain)
                .frame(minHeight: 32)
                if busy { CavnarShimmerText(text: "Sending\u{2026}", color: .cavnarEmber2) }
            }
            .disabled(busy)
        }
        .padding(14)
        .frame(maxWidth: .infinity, alignment: .leading)
        .background(Color.cavnarPaper2)
        .clipShape(RoundedRectangle(cornerRadius: CavnarRadius.control))
    }

    private func metricRow(_ maybe: [(String, String, Color)?]) -> some View {
        let items = maybe.compactMap { $0 }
        return HStack(alignment: .top, spacing: 14) {
            ForEach(Array(items.enumerated()), id: \.offset) { _, item in
                VStack(alignment: .leading, spacing: 2) {
                    Text(item.1)
                        .font(Int(item.1) != nil ? .cavnarNumber(15, weight: 700) : .cavnarBody(12.5, weight: 600))
                        .foregroundStyle(Int(item.1) != nil ? item.2 : Color.cavnarInk3)
                    Text(item.0).font(.cavnarBody(11)).foregroundStyle(Color.cavnarInk3)
                }
            }
        }
    }

    // MARK: - Settings

    private var settingsCard: some View {
        let o = viewModel.overview
        return VStack(alignment: .leading, spacing: 12) {
            HomeSectionHeader(kicker: "Settings", title: "The rules every campaign follows")
            settingRow("clock", "Sending hours",
                       SMSWindow.range(o?.window).map { "\($0), your time" } ?? "\u{2014}", always: true)
            settingRow("arrow.triangle.2.circlepath", "Spacing",
                       "One text per guest every \(mktPlural(o?.minDaysBetween ?? viewModel.ledger?.minDaysBetween ?? 3, "day"))",
                       always: true)
            settingRow("hand.raised", "Opt-outs", "STOP and HELP are handled for you", always: true)
            if let l = viewModel.ledger {
                settingRow("checkmark.shield", "Consent",
                           "\(l.textable) opted in \u{00B7} \(l.noConsent) without consent \u{00B7} \(l.unsubscribed) unsubscribed")
            }
            invitesRow
            settingRow("envelope", "Email", "Unsubscribe link on every email", always: true)
            settingRow("mappin.and.ellipse", "Mailing address",
                       (o?.mailingAddressSet ?? false) ? "On file \u{2014} printed on every email" : "Asked for on your first email")
            Button {
                Haptic.light()
                onOpenTextClub()
            } label: {
                HStack(spacing: 10) {
                    Image(systemName: "person.2").frame(width: 22).foregroundStyle(Color.cavnarEmber2)
                    VStack(alignment: .leading, spacing: 2) {
                        Text("Contacts, QR code and join link").font(.cavnarBody(15, weight: 700)).foregroundStyle(Color.cavnarInk)
                        Text("The Guest Text Club").font(.cavnarBody(CavnarType.caption)).foregroundStyle(Color.cavnarInk3)
                    }
                    Spacer()
                    Image(systemName: "chevron.right").font(.system(size: 13, weight: .semibold)).foregroundStyle(Color.cavnarInk3)
                }
                .frame(minHeight: 44)
                .contentShape(Rectangle())
            }
            .buttonStyle(.plain)
        }
        .cavnarCard()
    }

    private func settingRow(_ icon: String, _ title: String, _ value: String, always: Bool = false) -> some View {
        HStack(alignment: .firstTextBaseline, spacing: 10) {
            Image(systemName: icon).frame(width: 22).foregroundStyle(Color.cavnarEmber2)
            VStack(alignment: .leading, spacing: 2) {
                Text(title).font(.cavnarBody(15, weight: 700)).foregroundStyle(Color.cavnarInk)
                HomeMixedText.make(value, size: CavnarType.secondary, weight: 500, color: .cavnarInk3)
                    .fixedSize(horizontal: false, vertical: true)
            }
            Spacer(minLength: 6)
            if always {
                Text("Always on").font(.cavnarBody(11.5, weight: 700)).foregroundStyle(Color.cavnarGreen)
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
                        Text("Review-link invites").font(.cavnarBody(15, weight: 700)).foregroundStyle(Color.cavnarInk)
                        Text(inv.enabled ? "On \u{2014} one invite each afternoon to guests from your last service" : "Off")
                            .font(.cavnarBody(CavnarType.secondary))
                            .foregroundStyle(Color.cavnarInk3)
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
                        .disabled(!isOwner || viewModel.invitesBusy)
                        .accessibilityLabel("Text guests from your last service one review-link invite")
                }
                if !isOwner {
                    Text("Only the account owner can turn invite texts on or off.")
                        .font(.cavnarBody(CavnarType.caption))
                        .foregroundStyle(Color.cavnarInk3)
                }
                if let error = viewModel.invitesError {
                    Text(error).font(.cavnarBody(CavnarType.caption)).foregroundStyle(Color.cavnarRed)
                }
            }
        }
    }

    private var disclosureSheet: some View {
        NavigationStack {
            ScrollView {
                VStack(alignment: .leading, spacing: 16) {
                    CampaignKicker(text: "Review-link invites")
                    Text("Before you turn this on")
                        .font(.cavnarHeadline(CavnarType.section))
                        .foregroundStyle(Color.cavnarInk)
                    Text(viewModel.invites?.disclosure ?? "")
                        .font(.cavnarBody(CavnarType.emphasis))
                        .foregroundStyle(Color.cavnarInk)
                        .fixedSize(horizontal: false, vertical: true)
                    Text("Turning it on records that you agreed to this, with who and when.")
                        .font(.cavnarBody(CavnarType.secondary))
                        .foregroundStyle(Color.cavnarInk3)
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
