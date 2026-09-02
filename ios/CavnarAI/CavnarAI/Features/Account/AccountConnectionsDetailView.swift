import SwiftUI

/// Opened from Account's "Connected apps" row. Was a single card of 5
/// read-only rows with no way to actually connect anything. Now each
/// connection is its own standalone card with real space between them,
/// a brand-colored icon tile (SF Symbols, not the companies' own
/// trademarked marks), and a real action: Google Business runs a full
/// mobile OAuth round trip (GMBConnectCoordinator), Toast takes a
/// credential pair (ToastConnectSheet) — Toast has no OAuth of its own.
/// Instagram/Facebook, Square, and Clover have no connect API wired up
/// yet, so their action is an honest "ask Cavnar to connect it" email
/// rather than a button that looks like it works and does nothing.
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
                    requestRow(
                        "Instagram & Facebook", systemImage: "camera.fill", tint: Color(red: 0.82, green: 0.14, blue: 0.56),
                        status: connections.instagram
                    )
                    requestRow(
                        "Square POS", systemImage: "square.fill", tint: .black,
                        status: connections.square
                    )
                    requestRow(
                        "Clover POS", systemImage: "leaf.fill", tint: Color(red: 0.19, green: 0.6, blue: 0.35),
                        status: connections.clover
                    )
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

    private var marks: [(name: String, symbol: String, tint: Color, status: ConnectionStatus)] {
        [
            ("Google", "building.2.fill", Color(red: 0.26, green: 0.52, blue: 0.96), connections.googleBusiness),
            ("Toast", "fork.knife", Color(red: 0.98, green: 0.35, blue: 0.15), connections.toast),
            ("Instagram", "camera.fill", Color(red: 0.82, green: 0.14, blue: 0.56), connections.instagram),
            ("Square", "square.fill", .black, connections.square),
            ("Clover", "leaf.fill", Color(red: 0.19, green: 0.6, blue: 0.35), connections.clover),
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

    /// The five app marks in a row — lit when connected, dimmed when not.
    private var marksRow: some View {
        HStack(spacing: 8) {
            ForEach(marks, id: \.name) { mark in
                Image(systemName: mark.symbol)
                    .font(.system(size: 17))
                    .foregroundStyle(.white)
                    .frame(maxWidth: .infinity)
                    .frame(height: 44)
                    .background(mark.tint)
                    .clipShape(RoundedRectangle(cornerRadius: 12))
                    .opacity(mark.status.connected ? 1 : 0.3)
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
            header("Google Business", systemImage: "building.2.fill", tint: Color(red: 0.26, green: 0.52, blue: 0.96), status: connections.googleBusiness)

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
            header("Toast POS", systemImage: "fork.knife", tint: Color(red: 0.98, green: 0.35, blue: 0.15), status: connections.toast)

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

    private func requestRow(_ label: String, systemImage: String, tint: Color, status: ConnectionStatus) -> some View {
        VStack(alignment: .leading, spacing: 14) {
            header(label, systemImage: systemImage, tint: tint, status: status)

            if !status.connected {
                Link(destination: contactURL(for: label)) {
                    HStack(spacing: 5) {
                        Text("Ask Cavnar to connect this")
                        Image(systemName: "arrow.up.right")
                            .font(.system(size: 10))
                    }
                    .font(.cavnarBody(15, weight: 600))
                    .foregroundStyle(Color.cavnarEmber)
                }
            }
        }
        .cavnarCard()
    }

    private func contactURL(for label: String) -> URL {
        let subject = "Connect \(label)"
        let encoded = subject.addingPercentEncoding(withAllowedCharacters: .urlQueryAllowed) ?? subject
        return URL(string: "mailto:will@cavnar.ai?subject=\(encoded)")!
    }

    // MARK: - Shared header

    private func header(_ label: String, systemImage: String, tint: Color, status: ConnectionStatus) -> some View {
        HStack(spacing: 13) {
            Image(systemName: systemImage)
                .font(.system(size: 17))
                .foregroundStyle(.white)
                .frame(width: 40, height: 40)
                .background(tint)
                .clipShape(RoundedRectangle(cornerRadius: 11))

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
