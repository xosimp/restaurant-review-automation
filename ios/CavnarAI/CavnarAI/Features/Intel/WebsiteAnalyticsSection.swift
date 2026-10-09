import SwiftUI
import Charts

/// "Your website" on the phone (parity audit 10/7/26 #52): the restaurant's
/// Google Analytics and Search Console, read every morning — the web's
/// Marketing card (mktWebsiteHtml).
///
/// The phone keeps the four figures and what moved (iOS readability round,
/// 10/8/26); the visits chart, where visits came from, the clicks and the
/// searches are on the web, and so is the connection itself (the
/// read-only address, the GA4 property, the Search Console site).
///
/// What moved is said against a typical day of the same weekday, with what
/// else happened that day beside it: the two happened together, which is
/// never proof one caused the other, and the card says so.
struct WebsiteAnalyticsSection: View {
    @State private var viewModel = WebsiteAnalyticsViewModel()

    var body: some View {
        VStack(alignment: .leading, spacing: CavnarSpace.m) {
            Text("Your website").cavnarText(.headline)
            if let s = viewModel.summary {
                content(s)
            } else if viewModel.isLoading {
                VStack(alignment: .leading, spacing: 8) {
                    CavnarSkeletonBar(height: 14).frame(width: 220)
                    CavnarSkeletonBar(height: 14).frame(width: 160)
                }
            } else {
                Text("Couldn\u{2019}t load your website figures \u{2014} pull to refresh.")
                    .cavnarText(.secondary)
            }
        }
        .task { await viewModel.load() }
    }

    @ViewBuilder
    private func content(_ s: WebsiteSummary) -> some View {
        if !s.connected {
            VStack(alignment: .leading, spacing: CavnarSpace.s) {
                Text("Connect your website\u{2019}s Google Analytics to see visits, Google searches and clicks to book or order here, lined up with your sales, games and posts.")
                    .cavnarText(.body)
                    .fixedSize(horizontal: false, vertical: true)
                CavnarWebLinkRow(title: "Connect your website", subtitle: "Google Analytics and Search Console",
                                 path: "account/integrations", actionLabel: "Edit on the web")
            }
        } else if !s.available {
            Text(s.error ?? ((s.configured ?? true)
                 ? "Cavnar AI is reading your website for the first time \u{2014} this fills in within a few minutes."
                 : "Website analytics is being switched on for Cavnar AI."))
                .cavnarText(.body)
                .fixedSize(horizontal: false, vertical: true)
            readNowRow(s)
        } else {
            totals(s)
            moves(s)
            readNowRow(s)
            VStack(alignment: .leading, spacing: 0) {
                CavnarWebLinkRow(title: "Visits, sources and searches",
                                 subtitle: "The daily chart, where visits came from, clicks and Google searches",
                                 path: "marketing/website", actionLabel: "Open on the web")
                CavnarWebLinkRow(title: "Website connection", subtitle: "Google Analytics and Search Console",
                                 path: "account/integrations")
            }
        }
    }

    // MARK: Figures

    private func totals(_ s: WebsiteSummary) -> some View {
        VStack(alignment: .leading, spacing: 10) {
            LazyVGrid(columns: [GridItem(.flexible(), spacing: 12), GridItem(.flexible(), spacing: 12)],
                      alignment: .leading, spacing: 14) {
                ForEach(Array(s.totals.prefix(4))) { t in
                    VStack(alignment: .leading, spacing: 3) {
                        Text(t.label).cavnarText(.secondary)
                        Text(Int(t.value.rounded()).formatted()).cavnarText(.figureM)
                        HomeMixedText.make(WebsiteSummary.changeLine(t), role: .caption,
                                           color: t.pct == nil ? .cavnarInk3 : ((t.pct ?? 0) >= 0 ? .cavnarGreen : .cavnarRedText))
                    }
                    .accessibilityElement(children: .combine)
                }
            }
        }
    }

    private func kicker(_ text: String) -> some View {
        CavnarKicker(text)
    }

    private static let movesShown = 3

    /// What moved: each day against a typical day of its weekday, and what
    /// else happened that day — they happened together; one need not have
    /// caused the other.
    private func moves(_ s: WebsiteSummary) -> some View {
        VStack(alignment: .leading, spacing: 10) {
            kicker("What moved")
            if s.signals.isEmpty {
                Text("Nothing unusual \u{2014} each day looks like a typical one of its weekday.")
                    .cavnarText(.body)
                    .fixedSize(horizontal: false, vertical: true)
            }
            ForEach(Array(s.signals.prefix(Self.movesShown))) { m in
                moveCard(m)
            }
            if s.signals.count > Self.movesShown {
                CavnarMoreDisclosure(hiddenCount: min(s.signals.count, 6) - Self.movesShown) {
                    ForEach(Array(s.signals.prefix(6).dropFirst(Self.movesShown))) { m in
                        moveCard(m)
                    }
                }
            }
            if !s.signals.isEmpty {
                Text("These happened on the same day. That doesn\u{2019}t mean one caused the other.")
                    .cavnarText(.caption)
                    .fixedSize(horizontal: false, vertical: true)
            }
            if let basis = s.basisSentence {
                Text(basis)
                    .cavnarText(.caption)
                    .fixedSize(horizontal: false, vertical: true)
            }
        }
    }

    private func moveCard(_ m: WebsiteSummary.Signal) -> some View {
        VStack(alignment: .leading, spacing: 8) {
            CavnarMixedText(m.text, role: .body, color: .cavnarInk)
            if let ctx = m.context, !ctx.isEmpty {
                CavnarMixedText("Same day: " + ctx.joined(separator: " \u{00B7} "), role: .secondary)
            }
        }
        .padding(12)
        .frame(maxWidth: .infinity, alignment: .leading)
        .background(Color.cavnarPaper2.opacity(0.6), in: RoundedRectangle(cornerRadius: 12))
        .overlay(alignment: .leading) {
            Rectangle().fill(m.isUp ? Color.cavnarGreen : Color.cavnarRed).frame(width: 3)
        }
        .clipShape(RoundedRectangle(cornerRadius: 12))
    }

    /// When it was last read, and Read now.
    private func readNowRow(_ s: WebsiteSummary) -> some View {
        VStack(alignment: .leading, spacing: 6) {
            HStack(spacing: 10) {
                if let at = s.syncedAt, !at.isEmpty {
                    HomeMixedText.make("Read \(CavnarDate.mdyLocal(at)) \u{00B7} again every morning", role: .caption)
                }
                Spacer(minLength: 0)
                if viewModel.canReadNow {
                    Button {
                        Haptic.light()
                        Task { await viewModel.readNow() }
                    } label: {
                        if viewModel.isSyncing {
                            CavnarWorkingLine(width: 60, color: .cavnarEmber2)
                        } else {
                            Text("Read now").cavnarText(.label, color: .cavnarEmber2)
                        }
                    }
                    .buttonStyle(.plain)
                    .frame(minHeight: 44)
                    .disabled(viewModel.isSyncing)
                }
            }
            if let note = viewModel.syncNote {
                Text(note).cavnarText(.secondary)
            }
        }
    }
}

/// The connection itself: Cavnar AI's read-only address to add in Google
/// Analytics (Viewer) and Search Console (Restricted), the two properties,
/// Save and check, Read now and Disconnect — the web's Account card.
struct WebsiteConnectSheet: View {
    let viewModel: WebsiteAnalyticsViewModel
    @Environment(\.dismiss) private var dismiss
    @State private var ga4 = ""
    @State private var gsc = ""
    @State private var copied = false
    @State private var confirmingDisconnect = false
    @State private var seeded = false

    private var conn: WebsiteConnection { viewModel.connection ?? WebsiteConnection() }

    var body: some View {
        NavigationStack {
            ScrollView {
                VStack(alignment: .leading, spacing: 22) {
                    if viewModel.connection == nil {
                        CavnarSkeletonBar(height: 14).frame(width: 220)
                    } else if !conn.configured {
                        Text("Website analytics is being switched on for Cavnar AI. You\u{2019}ll be able to connect it here shortly.")
                            .font(.cavnar(.body))
                            .foregroundStyle(Color.cavnarInk2)
                            .fixedSize(horizontal: false, vertical: true)
                    } else {
                        steps
                    }
                }
                .padding(20)
            }
            .cavnarModuleBackground()
            .navigationTitle("Your website")
            .navigationBarTitleDisplayMode(.inline)
            .toolbar { cavnarTitleToolbar("Your website") }
            .toolbar {
                ToolbarItem(placement: .cancellationAction) {
                    Button("Done") { dismiss() }
                }
            }
            .task {
                if viewModel.connection == nil { await viewModel.load() }
                seed()
            }
            .onChange(of: viewModel.connection?.ga4PropertyId) { _, _ in seed() }
            .confirmationDialog("Stop reading your website analytics?", isPresented: $confirmingDisconnect,
                                titleVisibility: .visible) {
                Button("Disconnect", role: .destructive) {
                    Task {
                        if await viewModel.disconnect() {
                            ga4 = ""
                            gsc = ""
                        }
                    }
                }
                Button("Cancel", role: .cancel) {}
            } message: {
                Text("What\u{2019}s already been read stays.")
            }
        }
    }

    private func seed() {
        guard !seeded, let c = viewModel.connection else { return }
        seeded = true
        ga4 = c.ga4PropertyId ?? ""
        gsc = c.gscSiteUrl ?? ""
    }

    @ViewBuilder
    private var steps: some View {
        let email = conn.serviceEmail ?? ""
        step(1, "Copy Cavnar AI\u{2019}s read-only address",
             "It can read your analytics and nothing else \u{2014} it can\u{2019}t change your site, your account or your settings.") {
            VStack(alignment: .leading, spacing: 10) {
                Text(email)
                    .font(.cavnar(.secondary))
                    .foregroundStyle(Color.cavnarInk)
                    .textSelection(.enabled)
                    .padding(10)
                    .frame(maxWidth: .infinity, alignment: .leading)
                    .background(Color.cavnarPaper2, in: RoundedRectangle(cornerRadius: 9))
                HStack(spacing: 10) {
                    Button {
                        UIPasteboard.general.string = email
                        Haptic.success()
                        copied = true
                    } label: {
                        Label(copied ? "Copied" : "Copy", systemImage: copied ? "checkmark" : "doc.on.doc")
                            .frame(maxWidth: .infinity)
                    }
                    .buttonStyle(CavnarSecondaryButtonStyle())
                    .disabled(email.isEmpty)
                    ShareLink(item: email) {
                        Label("Share", systemImage: "square.and.arrow.up")
                            .frame(maxWidth: .infinity)
                    }
                    .buttonStyle(CavnarSecondaryButtonStyle())
                    .disabled(email.isEmpty)
                }
            }
        }
        step(2, "Add it in Google Analytics",
             "Admin \u{2192} Property access management \u{2192} + \u{2192} Add users. Paste the address, choose Viewer, untick \u{201C}Notify new users by email\u{201D}, then Add.") { EmptyView() }
        step(3, "Add it in Search Console (optional)",
             "Settings \u{2192} Users and permissions \u{2192} Add user. Paste the address and choose Restricted. This adds what people searched on Google to find you.") { EmptyView() }
        step(4, "Tell Cavnar AI which ones", nil) {
            VStack(alignment: .leading, spacing: 14) {
                field("GA4 property ID", text: $ga4, prompt: "412345678", keyboard: .numberPad,
                      hint: "Google Analytics \u{2192} Admin \u{2192} Property details. A number, not the G- ID.")
                field("Search Console property", text: $gsc, prompt: "sc-domain:yoursite.com", keyboard: .URL,
                      hint: "Your domain (yoursite.com), or exactly as Search Console shows it: sc-domain:yoursite.com or https://yoursite.com/.")
                if !conn.canEdit {
                    Text("Only the account owner can change website analytics.")
                        .font(.cavnar(.secondary))
                        .foregroundStyle(Color.cavnarInk2)
                }
                if let error = viewModel.errorMessage {
                    Text(error).font(.cavnar(.secondary)).foregroundStyle(Color.cavnarRedText)
                        .fixedSize(horizontal: false, vertical: true)
                }
                ForEach(Array(conn.checkLines.enumerated()), id: \.offset) { _, line in
                    Label(line.text, systemImage: line.ok ? "checkmark.circle.fill" : "exclamationmark.triangle.fill")
                        .font(.cavnar(.secondary))
                        .foregroundStyle(line.ok ? Color.cavnarInk2 : Color.cavnarRed)
                        .fixedSize(horizontal: false, vertical: true)
                }
                Button {
                    Haptic.light()
                    Task {
                        if await viewModel.save(ga4: ga4, gsc: gsc) { Haptic.success() }
                    }
                } label: {
                    Group {
                        if viewModel.isSaving {
                            CavnarShimmerText(text: "Checking\u{2026}", color: .white)
                        } else {
                            Text("Save and check")
                        }
                    }
                    .frame(maxWidth: .infinity)
                }
                .buttonStyle(CavnarPrimaryButtonStyle(isDisabled: !conn.canEdit || viewModel.isSaving))
                .disabled(!conn.canEdit || viewModel.isSaving)
                if conn.isConnected {
                    HStack(spacing: 10) {
                        if conn.canSync {
                            Button {
                                Haptic.light()
                                Task { await viewModel.readNow() }
                            } label: {
                                Group {
                                    if viewModel.isSyncing {
                                        CavnarWorkingLine(width: 60)
                                    } else {
                                        Text("Read now")
                                    }
                                }
                                .frame(maxWidth: .infinity)
                            }
                            .buttonStyle(CavnarSecondaryButtonStyle())
                            .disabled(viewModel.isSyncing)
                        }
                        if conn.canEdit {
                            Button(role: .destructive) {
                                Haptic.light()
                                confirmingDisconnect = true
                            } label: {
                                Text("Disconnect").frame(maxWidth: .infinity)
                            }
                            .buttonStyle(CavnarSecondaryButtonStyle())
                            .foregroundStyle(Color.cavnarRed)
                        }
                    }
                    if let note = viewModel.syncNote {
                        Text(note).font(.cavnar(.secondary)).foregroundStyle(Color.cavnarInk2)
                    }
                    if let at = conn.syncedAt, !at.isEmpty, conn.checks == nil {
                        HomeMixedText.make("Last read \(CavnarDate.mdyLocal(at)). Cavnar AI reads it again every morning.", role: .secondary, color: .cavnarInk2)
                    }
                }
            }
        }
    }

    private func step<Extra: View>(_ n: Int, _ title: String, _ detail: String?,
                                   @ViewBuilder extra: () -> Extra) -> some View {
        HStack(alignment: .top, spacing: 14) {
            Text("\(n)")
                .font(.cavnar(.figureS))
                .foregroundStyle(Color.cavnarEmber2)
                .frame(width: 26, height: 26)
                .background(Circle().fill(Color.cavnarEmber.opacity(0.16)))
            VStack(alignment: .leading, spacing: 6) {
                Text(title).font(.cavnar(.label)).foregroundStyle(Color.cavnarInk)
                if let detail {
                    Text(detail).font(.cavnar(.secondary)).foregroundStyle(Color.cavnarInk2)
                        .fixedSize(horizontal: false, vertical: true)
                }
                extra()
            }
        }
    }

    private func field(_ label: String, text: Binding<String>, prompt: String, keyboard: UIKeyboardType,
                       hint: String) -> some View {
        VStack(alignment: .leading, spacing: 5) {
            Text(label).font(.cavnar(.label)).foregroundStyle(Color.cavnarInk2)
            TextField(prompt, text: text)
                .font(.cavnar(.body))
                .keyboardType(keyboard)
                .textInputAutocapitalization(.never)
                .autocorrectionDisabled()
                .padding(12)
                .background(Color.cavnarPaper2, in: RoundedRectangle(cornerRadius: 10))
                .overlay(RoundedRectangle(cornerRadius: 10).strokeBorder(Color.cavnarPaper3, lineWidth: 1))
                .disabled(!conn.canEdit)
            Text(hint).font(.cavnar(.caption)).foregroundStyle(Color.cavnarInk3)
                .fixedSize(horizontal: false, vertical: true)
        }
    }
}
