import SwiftUI

/// Opened from Account's "Connected apps" row. Was a single card of 5
/// read-only rows with no way to actually connect anything. Now each
/// connection is its own standalone card with real space between them,
/// the brand's own real mark (see ConnectionMark below — real SVGs/PNG
/// sourced from each brand's own public assets, not SF Symbol
/// approximations), and a real action where one exists: Google Business
/// runs a full mobile OAuth round trip (GMBConnectCoordinator), Toast
/// takes a credential pair (ToastConnectSheet) — Toast has no OAuth of
/// its own. Instagram/Facebook, Square, and Clover have no connect API
/// wired up yet, so they show status only, no dead-end CTA.
struct AccountConnectionsDetailView: View {
    let viewModel: AccountViewModel
    let connections: AccountConnections
    @State private var showingToastConnect = false

    var body: some View {
        NavigationStack {
            ScrollView {
                VStack(alignment: .leading, spacing: 18) {
                    hero
                    marksRow
                    googleRow
                    toastRow
                    requestRow("Instagram & Facebook", brand: .instagram, status: connections.instagram)
                    requestRow("Square POS", brand: .square, status: connections.square)
                    requestRow("Clover POS", brand: .clover, status: connections.clover)
                }
                .frame(maxWidth: .infinity, alignment: .leading)
                .padding(20)
            }
            .accountSheetChrome("Connections")
            .sheet(isPresented: $showingToastConnect) {
                ToastConnectSheet(viewModel: viewModel)
            }
        }
    }

    // MARK: - Identity (option A)

    private var marks: [(name: String, brand: ConnectionBrand, status: ConnectionStatus)] {
        [
            ("Google", .google, connections.googleBusiness),
            ("Toast", .toast, connections.toast),
            ("Instagram", .instagram, connections.instagram),
            ("Square", .square, connections.square),
            ("Clover", .clover, connections.clover),
        ]
    }

    private var connectedNames: [String] { marks.filter { $0.status.connected }.map(\.name) }

    private var hero: some View {
        AccountHero(title: connectedNames.isEmpty ? "Nothing connected yet" : connectedNames.joined(separator: " · ")) {
            GlowBadge(systemImage: "link", size: 64)
        } subtitle: {
            Text("\(connectedNames.count)").font(.cavnarNumber(15.5, weight: 600))
                + Text(" of ")
                + Text("5").font(.cavnarNumber(15.5, weight: 600))
                + Text(" apps connected")
        }
    }

    /// The five real brand marks in a row — lit when connected, dimmed
    /// when not.
    private var marksRow: some View {
        HStack(spacing: 8) {
            ForEach(marks, id: \.name) { mark in
                ConnectionMarkTile(brand: mark.brand, size: 44)
                    .opacity(mark.status.connected ? 1 : 0.35)
                    .overlay(alignment: .topTrailing) {
                        if mark.status.connected {
                            Circle().fill(Color.cavnarGreen)
                                .frame(width: 8, height: 8)
                                .overlay(Circle().strokeBorder(Color.cavnarPaper, lineWidth: 1.5))
                                .offset(x: 2, y: -2)
                        }
                    }
            }
        }
    }

    // MARK: - Google Business (real OAuth)

    private var googleRow: some View {
        VStack(alignment: .leading, spacing: 14) {
            header("Google Business", brand: .google, status: connections.googleBusiness)

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
                Text(error).font(.cavnarBody(15)).foregroundStyle(Color.cavnarRed)
            }

            if connections.googleBusiness.connected {
                Button("Disconnect", role: .destructive) {
                    Haptic.light()
                    Task { await viewModel.disconnectGoogleBusiness() }
                }
                .font(.cavnarBody(15, weight: 600))
            } else {
                // No manual Haptic.light() — CavnarPrimaryButtonStyle fires
                // its own press haptic, so this was doubling up.
                Button {
                    Task { await viewModel.connectGoogleBusiness() }
                } label: {
                    if viewModel.isConnectingGoogle {
                        CavnarShimmerText(text: "Connecting…")
                    } else {
                        Text("Connect")
                    }
                }
                .buttonStyle(CavnarPrimaryButtonStyle(isDisabled: viewModel.isConnectingGoogle))
                .disabled(viewModel.isConnectingGoogle)
            }
        }
        .cavnarCard()
    }

    // MARK: - Toast (credential form)

    private var toastRow: some View {
        VStack(alignment: .leading, spacing: 14) {
            header("Toast POS", brand: .toast, status: connections.toast)

            if connections.toast.connected {
                Button("Disconnect", role: .destructive) {
                    Haptic.light()
                    Task { await viewModel.disconnectToast() }
                }
                .font(.cavnarBody(15, weight: 600))
            } else {
                // Same — the style's own press haptic covers this.
                Button {
                    showingToastConnect = true
                } label: {
                    Text("Connect")
                }
                .buttonStyle(CavnarPrimaryButtonStyle(isDisabled: false))
            }
        }
        .cavnarCard()
    }

    // MARK: - Not yet self-serve (Instagram/Facebook, Square, Clover)

    private func requestRow(_ label: String, brand: ConnectionBrand, status: ConnectionStatus) -> some View {
        header(label, brand: brand, status: status)
            .cavnarCard()
    }

    // MARK: - Shared header

    private func header(_ label: String, brand: ConnectionBrand, status: ConnectionStatus) -> some View {
        HStack(spacing: 13) {
            ConnectionMarkTile(brand: brand, size: 40)

            VStack(alignment: .leading, spacing: 2) {
                Text(label).font(.cavnarBody(15.5, weight: 700)).foregroundStyle(Color.cavnarInk)
                if status.connected, let lastSynced = status.lastSynced {
                    Text("Last synced \(lastSynced)").font(.cavnarBody(15.5)).foregroundStyle(Color.cavnarInk3)
                } else if !status.connected {
                    Text("Not connected").font(.cavnarBody(15.5)).foregroundStyle(Color.cavnarInk3)
                }
            }

            Spacer()

            HStack(spacing: 5) {
                Circle()
                    .fill(status.connected ? Color.cavnarGreen : Color.cavnarInk3.opacity(0.4))
                    .frame(width: 6, height: 6)
                Text(status.connected ? "Connected" : "Off")
                    .font(.cavnarBody(15, weight: 600))
                    .foregroundStyle(status.connected ? Color.cavnarGreen : Color.cavnarInk3)
            }
        }
    }
}

// MARK: - Real brand marks

enum ConnectionBrand {
    case google, toast, instagram, square, clover
}

/// Each brand's own real mark on a light neutral tile — brand marks are
/// designed against a light ground (that's how Google/Instagram/Square
/// all publish their own guidelines), so this stays a fixed light tile
/// regardless of the app's own dark theme, the same way a "Sign in with
/// Google" button never goes dark either.
struct ConnectionMarkTile: View {
    let brand: ConnectionBrand
    var size: CGFloat = 40

    var body: some View {
        RoundedRectangle(cornerRadius: size * 0.27, style: .continuous)
            .fill(Color(red: 0.97, green: 0.965, blue: 0.955))
            .overlay(
                RoundedRectangle(cornerRadius: size * 0.27, style: .continuous)
                    .strokeBorder(Color.black.opacity(0.06), lineWidth: 1)
            )
            .frame(width: size, height: size)
            .overlay(mark.padding(size * 0.2))
            .clipShape(RoundedRectangle(cornerRadius: size * 0.27, style: .continuous))
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
            Image("SquareMark").resizable().aspectRatio(contentMode: .fit)
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
