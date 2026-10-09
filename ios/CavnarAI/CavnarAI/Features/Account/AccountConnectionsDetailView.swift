import SwiftUI

/// Opened from Account's "Connected apps" row (iOS readability round [72]).
/// The hero says how many are connected; then one row per provider — its
/// real brand mark (ConnectionMark below), its name, one status and the
/// one action that fits: Sync now and Disconnect for a connected one, a
/// Connect button where the phone can run the whole flow itself (Google
/// Business and Instagram & Facebook are one-tap OAuth round trips in a
/// browser sheet, GMBConnectCoordinator). Toast, Square and Clover take a
/// client ID and secret (or a merchant GUID and token) that nobody types
/// on a phone, so connecting one is a single "Connect a POS · Connect on
/// the web" row. RPOWER is set up by Cavnar AI and only taken off here, by
/// the owner. Every disconnect asks first.
struct AccountConnectionsDetailView: View {
    let viewModel: AccountViewModel
    let connections: AccountConnections
    @Environment(SessionStore.self) private var sessionStore
    /// Connecting or disconnecting Google and Instagram is the account
    /// owner's (403 owner_only for a teammate): a teammate sees the status,
    /// not a button that is refused.
    private var isOwner: Bool { sessionStore.currentUser?.isOwner == true }
    @State private var showingWebsiteConnect = false
    /// The Intel card's own model, so the connect sheet is the one Intel uses.
    @State private var websiteModel = WebsiteAnalyticsViewModel()
    /// A disconnect waiting on its confirm: what it disconnects and how.
    @State private var pendingDisconnect: (name: String, action: () async -> Void)?

    var body: some View {
        NavigationStack {
            ScrollView {
                VStack(alignment: .leading, spacing: CavnarSpace.l) {
                    hero
                    VStack(alignment: .leading, spacing: 0) {
                        googleRow
                        AccountRowDivider()
                        instagramRow
                        AccountRowDivider()
                        posRow("Toast POS", brand: .toast, provider: "toast", status: connections.toast,
                               disconnect: { await viewModel.disconnectToast() })
                        AccountRowDivider()
                        posRow("Square POS", brand: .square, provider: "square", status: connections.square,
                               disconnect: { await viewModel.disconnectSquare() })
                        AccountRowDivider()
                        posRow("Clover POS", brand: .clover, provider: "clover", status: connections.clover,
                               disconnect: { await viewModel.disconnectClover() })
                        rpowerRow
                        webAnalyticsRow
                    }
                    .accountCard()

                    if let error = viewModel.disconnectError {
                        Text(error).cavnarText(.secondary, color: .cavnarRedText)
                    }

                    // L3: a POS connection is a credential form — the web's.
                    if !posConnectedAll {
                        VStack(alignment: .leading, spacing: 0) {
                            CavnarWebLinkRow(
                                title: "Connect a POS",
                                subtitle: isOwner
                                    ? "Toast, Square and Clover connect with keys from your POS account."
                                    : "Only the account owner can connect a POS.",
                                path: "account/integrations",
                                actionLabel: "Connect on the web"
                            )
                        }
                        .accountCard()
                    }
                }
                .frame(maxWidth: .infinity, alignment: .leading)
                .padding(CavnarSpace.gutter)
            }
            .accountSheetChrome("Connections")
            .confirmationDialog(
                pendingDisconnect.map { "Disconnect \($0.name)?" } ?? "",
                isPresented: Binding(get: { pendingDisconnect != nil }, set: { if !$0 { pendingDisconnect = nil } }),
                titleVisibility: .visible
            ) {
                Button("Disconnect", role: .destructive) {
                    guard let pending = pendingDisconnect else { return }
                    Task { await pending.action(); Haptic.success() }
                }
                Button("Cancel", role: .cancel) {}
            } message: {
                Text(Self.disconnectMessage(pendingDisconnect?.name))
            }
        }
    }

    /// What a disconnect stops, by connection.
    static func disconnectMessage(_ name: String?) -> String {
        switch name {
        case "RPOWER POS":
            return "Sales and labor stop syncing from RPOWER. Connecting it again goes through Cavnar AI."
        case "Google Business":
            return "New reviews stop coming in and replies stop posting to Google until you connect it again."
        case "Instagram & Facebook":
            return "Posts and scheduled content stop going to Instagram and Facebook until you connect again."
        default:
            return "Sales and labor stop syncing from it until you connect it again."
        }
    }

    /// Every credential POS already connected — nothing for the web row to do.
    private var posConnectedAll: Bool {
        connections.toast.connected && connections.square.connected && connections.clover.connected
    }

    // MARK: - Identity

    private var hero: some View {
        let connected = connections.all.filter(\.connected).count
        return AccountHero(title: connected == 0 ? "Nothing connected yet" : "\(connected) of \(connections.all.count) connected") {
            GlowBadge(systemImage: "link", size: 64)
        } subtitle: {
            Text("Where Cavnar AI reads your reviews, sales and labor.")
        }
    }

    // MARK: - One row per provider

    /// Mark · name · one status line · the row's actions.
    private func providerRow<Mark: View, Actions: View>(
        _ name: String,
        @ViewBuilder mark: () -> Mark,
        connected: Bool,
        statusText: String?,
        tone: ConnectionStatus.SyncTone?,
        @ViewBuilder actions: () -> Actions
    ) -> some View {
        VStack(alignment: .leading, spacing: CavnarSpace.xs) {
            HStack(spacing: CavnarSpace.s) {
                mark()
                VStack(alignment: .leading, spacing: 2) {
                    Text(name).cavnarText(.label)
                    if let statusText {
                        Self.statusLine(statusText, tone: tone ?? (connected ? .good : .neutral))
                    } else {
                        Self.statusLine(connected ? "Connected" : "Not connected", tone: connected ? .good : .neutral)
                    }
                }
                Spacer(minLength: 0)
            }
            actions()
        }
        .padding(.vertical, CavnarSpace.s)
    }

    /// Sync now and Disconnect side by side — any login syncs (as on the
    /// web); only the owner disconnects, and only after the confirm.
    @ViewBuilder
    private func connectedActions(_ provider: String, name: String, disconnect: @escaping () async -> Void) -> some View {
        HStack(spacing: CavnarSpace.s) {
            Button {
                Task { await viewModel.syncNow(provider) }
            } label: {
                Group {
                    if viewModel.syncingProvider == provider {
                        CavnarShimmerText(text: "Syncing…")
                    } else {
                        Text("Sync now")
                    }
                }
                .frame(maxWidth: .infinity)
            }
            .buttonStyle(CavnarSecondaryButtonStyle(isDisabled: viewModel.syncingProvider == provider))
            .disabled(viewModel.syncingProvider == provider)
            if isOwner {
                Button {
                    Haptic.light()
                    pendingDisconnect = (name, disconnect)
                } label: {
                    Text("Disconnect").frame(maxWidth: .infinity)
                }
                .buttonStyle(CavnarSecondaryButtonStyle())
                .foregroundStyle(Color.cavnarRedText)
            }
        }
        if let message = viewModel.syncMessage[provider] {
            CavnarMixedText(message, role: .caption)
        }
    }

    /// Disconnect alone (an OAuth connection has nothing to sync by hand).
    private func disconnectButton(_ name: String, action: @escaping () async -> Void) -> some View {
        Button {
            Haptic.light()
            pendingDisconnect = (name, action)
        } label: {
            Text("Disconnect").frame(maxWidth: .infinity)
        }
        .buttonStyle(CavnarSecondaryButtonStyle())
        .foregroundStyle(Color.cavnarRedText)
    }

    // MARK: - Google Business (real OAuth)

    private var googleRow: some View {
        let g = connections.googleBusiness
        // The real fetch state, in the restaurant's clock (DH4-6):
        // "Checked 11:02am · next check 4pm". Where the reviews come from
        // (G9) when it isn't a Business Profile connection.
        let line: String? = g.fetchLine?.line
            ?? (g.source != "gbp" ? g.label : nil)
            ?? (g.connected ? g.lastSyncedText : nil)
        return providerRow("Google Business", mark: { ConnectionMarkTile(brand: .google, size: 40) },
                           connected: g.connected, statusText: line,
                           tone: g.fetchLine.map { ConnectionStatus.tone($0.tone) }) {
            // "Handshake" — dashes march between the seal and Google while
            // the OAuth round trip is in flight (see CavnarMotion).
            if viewModel.isConnectingGoogle {
                CavnarHandshake(
                    providerSymbol: "building.2.fill", providerTint: Color(red: 0.26, green: 0.52, blue: 0.96),
                    state: .connecting, caption: "Connecting · Google Business"
                )
                .padding(.vertical, 4)
            }
            if let error = viewModel.connectGoogleError {
                Text(error).cavnarText(.secondary, color: .cavnarRedText)
            }
            if !isOwner {
                ownerOnlyNote
            } else if g.connected {
                disconnectButton("Google Business") { await viewModel.disconnectGoogleBusiness() }
            } else {
                // No manual Haptic.light() — CavnarPrimaryButtonStyle fires
                // its own press haptic.
                Button {
                    Task { await viewModel.connectGoogleBusiness() }
                } label: {
                    Group {
                        if viewModel.isConnectingGoogle {
                            CavnarShimmerText(text: "Connecting…")
                        } else {
                            Text("Connect")
                        }
                    }
                    .frame(maxWidth: .infinity)
                }
                .buttonStyle(CavnarPrimaryButtonStyle(isDisabled: viewModel.isConnectingGoogle))
                .disabled(viewModel.isConnectingGoogle)
            }
        }
    }

    private var ownerOnlyNote: some View {
        Text("Only the account owner can connect or disconnect this.")
            .cavnarText(.caption)
            .fixedSize(horizontal: false, vertical: true)
    }

    // MARK: - Instagram & Facebook (Meta OAuth — the web's popup, as a sheet)

    private var instagramRow: some View {
        let ig = connections.instagram
        return providerRow("Instagram & Facebook", mark: { ConnectionMarkTile(brand: .instagram, size: 40) },
                           connected: ig.connected,
                           statusText: ig.connected ? "Connected \u{00B7} posts go to your page and account" : nil,
                           tone: nil) {
            if viewModel.isConnectingInstagram {
                CavnarHandshake(
                    providerSymbol: "camera.fill", providerTint: Color(red: 0.88, green: 0.30, blue: 0.55),
                    state: .connecting, caption: "Connecting · Instagram & Facebook"
                )
                .padding(.vertical, 4)
            }
            if let error = viewModel.connectInstagramError {
                Text(error).cavnarText(.secondary, color: .cavnarRedText)
            }
            if !isOwner {
                ownerOnlyNote
            } else if ig.connected {
                disconnectButton("Instagram & Facebook") { await viewModel.disconnectInstagram() }
            } else {
                Button {
                    Task { await viewModel.connectInstagram() }
                } label: {
                    Group {
                        if viewModel.isConnectingInstagram {
                            CavnarShimmerText(text: "Connecting…")
                        } else {
                            Text("Connect with Facebook")
                        }
                    }
                    .frame(maxWidth: .infinity)
                }
                .buttonStyle(CavnarPrimaryButtonStyle(isDisabled: viewModel.isConnectingInstagram))
                .disabled(viewModel.isConnectingInstagram)
            }
        }
    }

    // MARK: - Credential-pair POS connections (Toast, Square, Clover)

    /// Connected: its sync state, Sync now and Disconnect. Not connected:
    /// the status only — the "Connect a POS" web row below connects it.
    private func posRow(
        _ label: String,
        brand: ConnectionBrand,
        provider: String,
        status: ConnectionStatus,
        disconnect: @escaping () async -> Void
    ) -> some View {
        let line = status.posStatusLine(provider: provider, posLine: connections.posLine)
        return providerRow(label, mark: { ConnectionMarkTile(brand: brand, size: 40) },
                           connected: status.connected,
                           statusText: line?.text ?? (status.connected ? status.lastSyncedText : nil),
                           tone: line?.tone) {
            if status.connected {
                connectedActions(provider, name: label, disconnect: disconnect)
            }
        }
    }

    // MARK: - Sync state (G3)

    /// How the sync is actually going — a connection that stopped syncing
    /// reads stale here as it does on Home, admin and the status page; an
    /// error is red. The server's own sentence when it sent one (DH4-2:
    /// "Last sync 3:02am · Sales through 9/19/26", verbatim — already in
    /// the restaurant's clock); the older state-derived wording otherwise.
    /// Nothing for a state the server didn't send.
    @ViewBuilder
    static func syncStateLine(_ status: ConnectionStatus, provider: String,
                              posLine: ServerStatusLine?) -> some View {
        if let line = status.posStatusLine(provider: provider, posLine: posLine) {
            statusLine(line.text, tone: line.tone)
        }
    }

    /// A 6pt dot and the sentence, in the tone's colour: good green, warn
    /// amber, bad red, neutral Ink2.
    static func statusLine(_ text: String, tone: ConnectionStatus.SyncTone) -> some View {
        let color: Color = {
            switch tone {
            case .good: return .cavnarGreen
            case .warn: return .cavnarAmber
            case .bad: return .cavnarRedText
            case .neutral: return .cavnarInk2
            }
        }()
        return HStack(alignment: .firstTextBaseline, spacing: 6) {
            Circle().fill(color).frame(width: 6, height: 6).accessibilityHidden(true)
            HomeMixedText.make(text, role: .secondary, color: color)
                .fixedSize(horizontal: false, vertical: true)
        }
        .accessibilityElement(children: .combine)
    }

    /// RPOWER — connected by Cavnar AI from its vendor credentials, never
    /// from the phone — so a status row, and only once there is something
    /// to say (G3: it was missing from this list entirely).
    @ViewBuilder
    private var rpowerRow: some View {
        if let rp = connections.rpower, rp.connected || rp.error != nil || rp.lastSynced != nil {
            AccountRowDivider()
            let line = rp.posStatusLine(provider: "rpower", posLine: connections.posLine)
            providerRow("RPOWER POS", mark: { GlowBadge(systemImage: "server.rack", size: 40) },
                        connected: rp.connected,
                        statusText: line?.text ?? (rp.serverSyncLine == nil ? rp.lastSyncedText : nil),
                        tone: line?.tone) {
                Text("Set up by Cavnar AI from your RPOWER account \u{2014} contact us to change it.")
                    .cavnarText(.caption)
                    .fixedSize(horizontal: false, vertical: true)
                if rp.connected {
                    // The owner can take the store off, as on the web
                    // (principal only); connecting again is Cavnar AI's.
                    connectedActions("rpower", name: "RPOWER POS") { await viewModel.disconnectRPower() }
                }
            }
        }
    }

    /// Website analytics (GA4 / Search Console) — status here, and the
    /// owner connects or manages it in Intel's connect sheet. Counted with
    /// the rest (parity #80).
    @ViewBuilder
    private var webAnalyticsRow: some View {
        if let wa = connections.webAnalytics {
            AccountRowDivider()
            providerRow("Website analytics", mark: { GlowBadge(systemImage: "chart.bar.xaxis", size: 40) },
                        connected: wa.connected,
                        statusText: wa.connected ? wa.lastSyncedText : nil,
                        tone: nil) {
                // Connecting it is the owner's (the route answers 403
                // owner_only to anyone else): the Intel connect sheet, here
                // too (re-audit 10/8/26, #18).
                if isOwner {
                    Button {
                        Haptic.light()
                        showingWebsiteConnect = true
                    } label: {
                        Text(wa.connected ? "Manage the connection" : "Connect my website").frame(maxWidth: .infinity)
                    }
                    .buttonStyle(CavnarSecondaryButtonStyle())
                }
            }
            .sheet(isPresented: $showingWebsiteConnect, onDismiss: {
                // Connected or disconnected there: the count and this row re-read.
                Task { await viewModel.load() }
            }) {
                WebsiteConnectSheet(viewModel: websiteModel)
                    .presentationDetents([.large])
                    .presentationDragIndicator(.visible)
            }
        }
    }
}

// MARK: - Real brand marks

enum ConnectionBrand {
    case google, toast, instagram, square, clover
}

/// Each brand's own real mark on the exact same "obsidian tile" every
/// other badge in the app sits on (GlowBadge — Home's module tiles, every
/// Account sheet's hero icon, the Ask Cavnar FAB): the dark gradient
/// surface, ember-lit top-left edge, hairline top highlight, drop shadow,
/// and the ember dot seated on the right edge. First pass here was a flat
/// translucent-white tile, its own one-off treatment that matched nothing
/// else in the app — device feedback asked for the real shared badge
/// look. GlowBadge itself only ever draws an SF Symbol or a text
/// monogram, not arbitrary art, so this duplicates its tile construction
/// rather than modifying a component used this widely — touching
/// GlowBadge itself would mean re-verifying every screen that already
/// uses it (Home, every Account hero, the FAB) for a change scoped to
/// Connections alone.
///
/// Google/Toast/Instagram/Clover's marks are all transparent-background
/// art that reads fine on dark; Square's is the one exception — Simple
/// Icons' asset is a flat dark charcoal (#3E4348) shape meant for a light
/// ground, which would all but disappear here, so it renders as a
/// template and gets tinted light instead of using its own baked-in color.
struct ConnectionMarkTile: View {
    let brand: ConnectionBrand
    var size: CGFloat = 40

    private var radius: CGFloat { size * 0.3 }
    private var shape: RoundedRectangle { RoundedRectangle(cornerRadius: radius, style: .continuous) }

    // Obsidian — same two stops as GlowBadge, matching the app icon's own
    // dark surface family.
    private static let tileTop = Color(red: 0.173, green: 0.173, blue: 0.180)
    private static let tileBottom = Color(red: 0.086, green: 0.086, blue: 0.094)

    var body: some View {
        ZStack {
            shape
                .fill(LinearGradient(colors: [Self.tileTop, Self.tileBottom], startPoint: .top, endPoint: .bottom))
                .frame(width: size, height: size)
                .overlay(
                    shape.strokeBorder(
                        LinearGradient(
                            stops: [
                                .init(color: Color.cavnarEmber2.opacity(0.95), location: 0),
                                .init(color: Color.cavnarEmber.opacity(0.4), location: 0.45),
                                .init(color: Color.cavnarEmber.opacity(0.12), location: 1),
                            ],
                            startPoint: .topLeading, endPoint: .bottomTrailing
                        ),
                        lineWidth: max(1, size * 0.03)
                    )
                )
                .overlay(
                    shape
                        .inset(by: max(1, size * 0.03) + 0.5)
                        .strokeBorder(
                            LinearGradient(colors: [Color.white.opacity(0.10), Color.white.opacity(0)], startPoint: .top, endPoint: .center),
                            lineWidth: 1
                        )
                )
                .shadow(color: .black.opacity(0.55), radius: size * 0.1, x: 0, y: size * 0.06)

            mark
                .padding(size * 0.22)
                .frame(width: size, height: size)

            Circle()
                .fill(
                    RadialGradient(
                        colors: [Color.cavnarEmber2, Color.cavnarEmber],
                        center: UnitPoint(x: 0.42, y: 0.38), startRadius: 0, endRadius: size * 0.1
                    )
                )
                .frame(width: size * 0.17, height: size * 0.17)
                .offset(x: size * 0.5 - size * 0.055)
        }
        .frame(width: size, height: size)
        .compositingGroup()
    }

    @ViewBuilder
    private var mark: some View {
        switch brand {
        case .google:
            // Real 4-color "G" — Wikimedia's copy of Google's own
            // publicly-published brand mark (used in every "Sign in with
            // Google" button), fetched and bundled as GoogleMark.
            Image("GoogleMark").resizable().aspectRatio(contentMode: .fit)
        case .instagram:
            // Real Instagram glyph shape, masked with their actual
            // signature gradient (purple -> pink -> orange) instead of
            // Simple Icons' single flat brand pink — the gradient is what
            // people actually recognize as "Instagram."
            Image("InstagramMark")
                .resizable()
                .renderingMode(.template)
                .aspectRatio(contentMode: .fit)
                .foregroundStyle(
                    LinearGradient(
                        colors: [
                            Color(red: 0.51, green: 0.22, blue: 0.93),
                            Color(red: 0.89, green: 0.15, blue: 0.42),
                            Color(red: 0.98, green: 0.53, blue: 0.13),
                        ],
                        startPoint: .topLeading, endPoint: .bottomTrailing
                    )
                )
        case .square:
            Image("SquareMark")
                .resizable()
                .renderingMode(.template)
                .aspectRatio(contentMode: .fit)
                .foregroundStyle(Color.white.opacity(0.92))
        case .toast:
            Image("ToastMark").resizable().aspectRatio(contentMode: .fit)
        case .clover:
            CloverMark()
        }
    }
}

/// Clover's real mark IS four overlapping circles (with a small light
/// notch cut into the bottom-left leaf) — reconstructed as native vector
/// geometry from their own app icon rather than an approximated raster,
/// since no clean vector source was available. Color sampled directly
/// from their published app icon.
private struct CloverMark: View {
    private static let cloverGreen = Color(red: 0.137, green: 0.471, blue: 0.004)

    var body: some View {
        GeometryReader { geo in
            let s = min(geo.size.width, geo.size.height)
            let r = s * 0.27
            let offset = r * 0.92
            ZStack {
                leaf(r: r).offset(x: -offset, y: -offset)
                leaf(r: r).offset(x: offset, y: -offset)
                leaf(r: r).offset(x: -offset, y: offset)
                leaf(r: r, notched: true).offset(x: offset, y: offset)
            }
            .frame(width: geo.size.width, height: geo.size.height)
        }
    }

    @ViewBuilder
    private func leaf(r: CGFloat, notched: Bool = false) -> some View {
        if notched {
            Circle()
                .fill(Self.cloverGreen)
                .frame(width: r * 2, height: r * 2)
                .overlay(
                    Circle()
                        .trim(from: 0.5, to: 0.75)
                        .stroke(Color.white.opacity(0.55), lineWidth: r * 0.22)
                        .frame(width: r * 1.15, height: r * 1.15)
                )
        } else {
            Circle().fill(Self.cloverGreen).frame(width: r * 2, height: r * 2)
        }
    }
}
