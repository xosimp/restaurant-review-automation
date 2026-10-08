import SwiftUI
import Charts

/// "Your website" on the phone (parity audit 10/7/26 #52): the restaurant's
/// Google Analytics and Search Console, read every morning — the web's
/// Marketing card (mktWebsiteHtml) — with the connection behind it in a
/// sheet (the web's Account card: the read-only address to copy or share,
/// the GA4 property, the Search Console site, Read now, Disconnect).
///
/// What moved is said against a typical day of the same weekday, with what
/// else happened that day beside it: the two moved together, which is never
/// proof one caused the other, and the card says so.
struct WebsiteAnalyticsSection: View {
    @State private var viewModel = WebsiteAnalyticsViewModel()
    @State private var showingConnect = false

    var body: some View {
        VStack(alignment: .leading, spacing: 16) {
            HStack(alignment: .firstTextBaseline) {
                Text("YOUR WEBSITE")
                    .font(.cavnarBody(14, weight: 700))
                    .tracking(1.2)
                    .foregroundStyle(Color.cavnarEmber)
                Spacer()
                if viewModel.summary != nil || viewModel.connection != nil {
                    Button {
                        Haptic.light()
                        showingConnect = true
                    } label: {
                        HStack(spacing: 4) {
                            Image(systemName: (viewModel.connection?.isConnected ?? false) ? "gearshape" : "link")
                                .font(.system(size: 10, weight: .bold))
                            Text((viewModel.connection?.isConnected ?? viewModel.summary?.connected ?? false)
                                 ? "Connection" : "Connect")
                        }
                        .font(.cavnarBody(14, weight: 700))
                        .foregroundStyle(Color.cavnarEmber2)
                        .padding(.horizontal, 11)
                        .frame(minHeight: 32)
                        .overlay(Capsule().strokeBorder(Color.cavnarEmber2.opacity(0.4), lineWidth: 1))
                        .frame(minHeight: 44)
                        .contentShape(Rectangle())
                    }
                    .buttonStyle(.plain)
                }
            }
            if let s = viewModel.summary {
                content(s)
            } else if viewModel.isLoading {
                VStack(alignment: .leading, spacing: 8) {
                    CavnarSkeletonBar(height: 14).frame(width: 220)
                    CavnarSkeletonBar(height: 14).frame(width: 160)
                }
            } else {
                Text("Couldn\u{2019}t load your website figures \u{2014} pull to refresh.")
                    .font(.cavnarBody(14))
                    .foregroundStyle(Color.cavnarInk3)
            }
        }
        .task { await viewModel.load() }
        .sheet(isPresented: $showingConnect) {
            WebsiteConnectSheet(viewModel: viewModel)
                .presentationDetents([.large])
                .presentationDragIndicator(.visible)
        }
    }

    @ViewBuilder
    private func content(_ s: WebsiteSummary) -> some View {
        if !s.connected {
            VStack(alignment: .leading, spacing: 12) {
                Text("Connect your website\u{2019}s Google Analytics to see visits, Google searches and clicks to book or order here, lined up with your sales, games and posts.")
                    .font(.cavnarBody(15))
                    .foregroundStyle(Color.cavnarInk2)
                    .fixedSize(horizontal: false, vertical: true)
                Button {
                    Haptic.light()
                    showingConnect = true
                } label: {
                    Text("Connect my website").frame(maxWidth: .infinity)
                }
                .buttonStyle(CavnarSecondaryButtonStyle())
            }
        } else if !s.available {
            Text(s.error ?? ((s.configured ?? true)
                 ? "Cavnar AI is reading your website for the first time \u{2014} this fills in within a few minutes."
                 : "Website analytics is being switched on for Cavnar AI."))
                .font(.cavnarBody(15))
                .foregroundStyle(Color.cavnarInk2)
                .fixedSize(horizontal: false, vertical: true)
            readNowRow(s)
        } else {
            totals(s)
            visitsChart(s)
            if !s.channels.isEmpty { channels(s) }
            if !s.clicks.isEmpty { clicks(s) }
            if !s.queries.isEmpty { queries(s) }
            moves(s)
            readNowRow(s)
        }
    }

    // MARK: Figures

    private func totals(_ s: WebsiteSummary) -> some View {
        VStack(alignment: .leading, spacing: 10) {
            LazyVGrid(columns: [GridItem(.flexible(), spacing: 12), GridItem(.flexible(), spacing: 12)],
                      alignment: .leading, spacing: 14) {
                ForEach(Array(s.totals.prefix(4))) { t in
                    VStack(alignment: .leading, spacing: 3) {
                        Text(t.label)
                            .font(.cavnarBody(12.5, weight: 600))
                            .foregroundStyle(Color.cavnarInk3)
                        Text(Int(t.value.rounded()).formatted())
                            .font(.cavnarNumber(24, weight: 700))
                            .foregroundStyle(Color.cavnarInk)
                        HomeMixedText.make(WebsiteSummary.changeLine(t), size: 12, weight: 600,
                                           color: t.pct == nil ? .cavnarInk3 : ((t.pct ?? 0) >= 0 ? .cavnarGreen : .cavnarRed))
                    }
                    .accessibilityElement(children: .combine)
                }
            }
            if s.totals.count > 4 {
                HomeMixedText.make(s.totals.dropFirst(4).map { t in
                    "\(t.label) \(Int(t.value.rounded()).formatted())"
                        + (t.pct.map { " (\($0 >= 0 ? "+" : "")\($0)%)" } ?? "")
                }.joined(separator: " \u{00B7} "), size: 13, weight: 500, color: .cavnarInk3)
                .fixedSize(horizontal: false, vertical: true)
            }
        }
    }

    /// Website visits a day, the last 28 — a missing day is a gap, not 0.
    @ViewBuilder
    private func visitsChart(_ s: WebsiteSummary) -> some View {
        let points = s.line.filter { $0.value != nil }
        if points.count > 6, let first = s.line.first, let last = s.line.last {
            VStack(alignment: .leading, spacing: 4) {
                Chart(points) { p in
                    AreaMark(x: .value("Day", p.day), y: .value("Visits", p.value ?? 0))
                        .foregroundStyle(LinearGradient(colors: [Color.cavnarEmber.opacity(0.35), Color.cavnarEmber.opacity(0.02)],
                                                        startPoint: .top, endPoint: .bottom))
                        .interpolationMethod(.monotone)
                    LineMark(x: .value("Day", p.day), y: .value("Visits", p.value ?? 0))
                        .foregroundStyle(Color.cavnarEmber2)
                        .lineStyle(StrokeStyle(lineWidth: 2.2, lineCap: .round))
                        .interpolationMethod(.monotone)
                        .shadow(color: Color.cavnarEmber.opacity(0.6), radius: 4)
                }
                .chartXAxis(.hidden)
                .chartYAxis {
                    AxisMarks(position: .leading, values: .automatic(desiredCount: 3)) { v in
                        AxisGridLine().foregroundStyle(Color.cavnarPaper3.opacity(0.5))
                        AxisValueLabel {
                            if let n = v.as(Double.self) {
                                Text(Int(n).formatted()).font(.cavnarNumber(11)).foregroundStyle(Color.cavnarInk3)
                            }
                        }
                    }
                }
                .frame(height: 120)
                .accessibilityLabel("Website visits a day, \(CavnarDate.mdy(first.day)) to \(CavnarDate.mdy(last.day))")
                HStack {
                    Text(CavnarDate.mdy(first.day)).font(.cavnarNumber(12)).foregroundStyle(Color.cavnarInk3)
                    Spacer()
                    Text("Website visits a day").font(.cavnarBody(12)).foregroundStyle(Color.cavnarInk3)
                    Spacer()
                    Text(CavnarDate.mdy(last.day)).font(.cavnarNumber(12)).foregroundStyle(Color.cavnarInk3)
                }
            }
        }
    }

    private func kicker(_ text: String) -> some View {
        Text(text.uppercased())
            .font(.cavnarBody(CavnarType.kicker, weight: 700))
            .tracking(1.0)
            .foregroundStyle(Color.cavnarEmber)
    }

    private func channels(_ s: WebsiteSummary) -> some View {
        VStack(alignment: .leading, spacing: 10) {
            kicker("Where visits came from")
            ForEach(s.channels) { c in
                VStack(alignment: .leading, spacing: 4) {
                    HStack {
                        Text(c.name).font(.cavnarBody(14.5)).foregroundStyle(Color.cavnarInk2)
                        Spacer()
                        Text(c.share.map { "\($0)%" } ?? "\u{2014}")
                            .font(.cavnarNumber(14, weight: 600)).foregroundStyle(Color.cavnarInk)
                    }
                    GeometryReader { geo in
                        ZStack(alignment: .leading) {
                            Capsule().fill(Color.cavnarPaper3.opacity(0.5))
                            Capsule()
                                .fill(LinearGradient(colors: [Color.cavnarEmber2, Color.cavnarEmber],
                                                     startPoint: .leading, endPoint: .trailing))
                                .frame(width: geo.size.width * CGFloat(max(2, c.share ?? 0)) / 100)
                                .shadow(color: Color.cavnarEmber.opacity(0.5), radius: 5)
                        }
                    }
                    .frame(height: 6)
                }
                .accessibilityElement(children: .combine)
            }
        }
    }

    private func clicks(_ s: WebsiteSummary) -> some View {
        VStack(alignment: .leading, spacing: 8) {
            kicker("Clicks out of the site")
            ForEach(Array(s.clicks.prefix(5))) { c in
                HStack {
                    Text(WebsiteSummary.clickLabel(c)).font(.cavnarBody(14.5)).foregroundStyle(Color.cavnarInk2)
                    Spacer()
                    Text(c.value.map { Int($0.rounded()).formatted() } ?? "\u{2014}")
                        .font(.cavnarNumber(14, weight: 600)).foregroundStyle(Color.cavnarInk)
                }
            }
        }
    }

    private func queries(_ s: WebsiteSummary) -> some View {
        VStack(alignment: .leading, spacing: 8) {
            kicker("Top Google searches")
            ForEach(Array(s.queries.prefix(8))) { q in
                HStack(alignment: .firstTextBaseline) {
                    Text(q.query).font(.cavnarBody(14.5)).foregroundStyle(Color.cavnarInk2)
                    Spacer(minLength: 10)
                    HomeMixedText.make("\(q.clicks.formatted()) clicks", size: 13.5, weight: 500, color: .cavnarInk3,
                                       numberColor: .cavnarInk)
                }
                .padding(.vertical, 3)
                .overlay(alignment: .bottom) { Rectangle().fill(Color.cavnarPaper3.opacity(0.5)).frame(height: 1) }
            }
            if let through = s.queriesThrough {
                HomeMixedText.make("The 28 days to \(through) (Search Console runs 2\u{2013}3 days behind)",
                                   size: 12.5, weight: 500, color: .cavnarInk3)
            }
        }
    }

    /// What moved: each day against a typical day of its weekday, and what
    /// else happened that day — moved together, never caused.
    private func moves(_ s: WebsiteSummary) -> some View {
        VStack(alignment: .leading, spacing: 10) {
            kicker("What moved")
            if s.signals.isEmpty {
                Text("Nothing unusual \u{2014} each day looks like a typical one of its weekday.")
                    .font(.cavnarBody(14.5))
                    .foregroundStyle(Color.cavnarInk2)
                    .fixedSize(horizontal: false, vertical: true)
            }
            ForEach(Array(s.signals.prefix(6))) { m in
                VStack(alignment: .leading, spacing: 8) {
                    HomeMixedText.make(m.text, size: 14.5, weight: 500, color: .cavnarInk)
                        .fixedSize(horizontal: false, vertical: true)
                    if let ctx = m.context, !ctx.isEmpty {
                        ScrollView(.horizontal, showsIndicators: false) {
                            HStack(spacing: 6) {
                                Text("Same day:").font(.cavnarBody(12.5, weight: 600)).foregroundStyle(Color.cavnarInk3)
                                ForEach(ctx, id: \.self) { c in
                                    HomeMixedText.make(c, size: 12.5, weight: 500, color: .cavnarInk2)
                                        .padding(.horizontal, 10).padding(.vertical, 3)
                                        .overlay(Capsule().strokeBorder(Color.cavnarPaper3, lineWidth: 1))
                                }
                            }
                        }
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
            Text("Moved together, never caused: what else happened that day is beside it, not the reason for it.")
                .font(.cavnarBody(12.5, weight: 600))
                .foregroundStyle(Color.cavnarInk3)
                .fixedSize(horizontal: false, vertical: true)
            if let basis = s.basisSentence {
                Text(basis)
                    .font(.cavnarBody(12.5))
                    .foregroundStyle(Color.cavnarInk3)
                    .fixedSize(horizontal: false, vertical: true)
            }
        }
    }

    /// When it was last read, and Read now.
    private func readNowRow(_ s: WebsiteSummary) -> some View {
        VStack(alignment: .leading, spacing: 6) {
            HStack(spacing: 10) {
                if let at = s.syncedAt, !at.isEmpty {
                    HomeMixedText.make("Read \(CavnarDate.mdyLocal(at)) \u{00B7} again every morning",
                                       size: 12.5, weight: 500, color: .cavnarInk3)
                }
                Spacer(minLength: 0)
                Button {
                    Haptic.light()
                    Task { await viewModel.readNow() }
                } label: {
                    if viewModel.isSyncing {
                        CavnarWorkingLine(width: 60, color: .cavnarEmber2)
                    } else {
                        Text("Read now")
                            .font(.cavnarBody(14, weight: 700))
                            .foregroundStyle(Color.cavnarEmber2)
                    }
                }
                .buttonStyle(.plain)
                .frame(minHeight: 44)
                .disabled(viewModel.isSyncing)
            }
            if let note = viewModel.syncNote {
                Text(note).font(.cavnarBody(13)).foregroundStyle(Color.cavnarInk2)
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
                            .font(.cavnarBody(15))
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
                    .font(.cavnarBody(14))
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
                        .font(.cavnarBody(13.5))
                        .foregroundStyle(Color.cavnarInk3)
                }
                if let error = viewModel.errorMessage {
                    Text(error).font(.cavnarBody(14)).foregroundStyle(Color.cavnarRed)
                        .fixedSize(horizontal: false, vertical: true)
                }
                ForEach(Array(conn.checkLines.enumerated()), id: \.offset) { _, line in
                    Label(line.text, systemImage: line.ok ? "checkmark.circle.fill" : "exclamationmark.triangle.fill")
                        .font(.cavnarBody(14))
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
                        Text(note).font(.cavnarBody(13.5)).foregroundStyle(Color.cavnarInk2)
                    }
                    if let at = conn.syncedAt, !at.isEmpty, conn.checks == nil {
                        HomeMixedText.make("Last read \(CavnarDate.mdyLocal(at)). Cavnar AI reads it again every morning.",
                                           size: 13.5, weight: 500, color: .cavnarInk3)
                    }
                }
            }
        }
    }

    private func step<Extra: View>(_ n: Int, _ title: String, _ detail: String?,
                                   @ViewBuilder extra: () -> Extra) -> some View {
        HStack(alignment: .top, spacing: 14) {
            Text("\(n)")
                .font(.cavnarNumber(13, weight: 700))
                .foregroundStyle(Color.cavnarEmber)
                .frame(width: 26, height: 26)
                .background(Circle().fill(Color.cavnarEmber.opacity(0.16)))
            VStack(alignment: .leading, spacing: 6) {
                Text(title).font(.cavnarBody(15.5, weight: 700)).foregroundStyle(Color.cavnarInk)
                if let detail {
                    Text(detail).font(.cavnarBody(14.5)).foregroundStyle(Color.cavnarInk2)
                        .fixedSize(horizontal: false, vertical: true)
                }
                extra()
            }
        }
    }

    private func field(_ label: String, text: Binding<String>, prompt: String, keyboard: UIKeyboardType,
                       hint: String) -> some View {
        VStack(alignment: .leading, spacing: 5) {
            Text(label).font(.cavnarBody(13.5, weight: 600)).foregroundStyle(Color.cavnarInk2)
            TextField(prompt, text: text)
                .font(.cavnarBody(16))
                .keyboardType(keyboard)
                .textInputAutocapitalization(.never)
                .autocorrectionDisabled()
                .padding(12)
                .background(Color.cavnarPaper2, in: RoundedRectangle(cornerRadius: 10))
                .overlay(RoundedRectangle(cornerRadius: 10).strokeBorder(Color.cavnarPaper3, lineWidth: 1))
                .disabled(!conn.canEdit)
            Text(hint).font(.cavnarBody(13)).foregroundStyle(Color.cavnarInk3)
                .fixedSize(horizontal: false, vertical: true)
        }
    }
}
