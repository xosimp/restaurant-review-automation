import SwiftUI

private enum SquareConnectField: Hashable, CaseIterable {
    case accessToken, locationId
}

/// Self-service Square connect — the same two fields (access token +
/// location ID) the admin panel and the web dashboard already set. Like
/// Toast, Square's own OAuth isn't wired up here, so a real credential
/// pair is the actual "connect" action. The credentials are verified
/// against Square before anything is stored, so a typo fails in this
/// sheet rather than silently at the next nightly sync.
struct SquareConnectSheet: View {
    let viewModel: AccountViewModel
    @Environment(\.dismiss) private var dismiss
    @State private var accessToken = ""
    @State private var locationId = ""
    @FocusState private var focusedField: SquareConnectField?
    // "Handshake" (see CavnarMotion) — marching dashes while verifying,
    // solid ember on success (held a beat before the sheet dismisses),
    // stopped red line on failure until the next attempt.
    @State private var handshake: CavnarHandshakeState?

    private var canSubmit: Bool {
        !viewModel.isConnectingSquare && !accessToken.isEmpty && !locationId.isEmpty
    }

    var body: some View {
        NavigationStack {
            ScrollView {
                VStack(alignment: .leading, spacing: 26) {
                    Text("Find these in Square's Developer Dashboard — create an application, then copy its access token and the location ID you want to sync.")
                        .font(.cavnarBody(15))
                        .foregroundStyle(Color.cavnarInk3)

                    CavnarFloatingField(
                        icon: "key", placeholder: "Access token", text: $accessToken,
                        isSecure: true, autocapitalization: .never, focus: $focusedField, field: .accessToken
                    )
                    CavnarFloatingField(
                        icon: "mappin.and.ellipse", placeholder: "Location ID", text: $locationId,
                        autocapitalization: .never, focus: $focusedField, field: .locationId
                    )

                    if let handshake {
                        CavnarHandshake(
                            providerSymbol: "square.grid.2x2.fill",
                            providerTint: Color(red: 0.0, green: 0.0, blue: 0.0),
                            state: handshake, caption: handshakeCaption(handshake)
                        )
                        .transition(.opacity)
                    }

                    if let error = viewModel.connectSquareError {
                        Text(error).font(.cavnarBody(15)).foregroundStyle(Color.cavnarRed)
                    }

                    // Plain full-width buttons, not CavnarFormButtonPair —
                    // see SendReviewRequestSheet's identical comment; same
                    // PreferenceKey width-matching bug, same fix.
                    VStack(spacing: 10) {
                        Button {
                            Task {
                                withAnimation(.easeOut(duration: 0.25)) { handshake = .connecting }
                                await viewModel.connectSquare(accessToken: accessToken, locationId: locationId)
                                if viewModel.connectSquareSucceeded {
                                    handshake = .connected
                                    try? await Task.sleep(for: .seconds(1.2))
                                    dismiss()
                                } else {
                                    handshake = .failed
                                }
                            }
                        } label: {
                            Group {
                                if viewModel.isConnectingSquare {
                                    CavnarShimmerText(text: "Connecting…")
                                } else {
                                    Text("Connect Square")
                                }
                            }
                            .frame(maxWidth: .infinity)
                        }
                        .buttonStyle(CavnarPrimaryButtonStyle(isDisabled: !canSubmit))
                        .disabled(!canSubmit)

                        Button {
                            dismiss()
                        } label: {
                            Text("Cancel").frame(maxWidth: .infinity)
                        }
                        .buttonStyle(CavnarSecondaryButtonStyle())
                    }
                    .padding(.top, 6)
                }
                .padding(20)
            }
            .cavnarModuleBackground()
            .navigationTitle("Connect Square")
            .navigationBarTitleDisplayMode(.inline)
            .toolbar { cavnarTitleToolbar("Connect Square") }
            .keyboardNavToolbar($focusedField)
        }
    }

    private func handshakeCaption(_ state: CavnarHandshakeState) -> String {
        switch state {
        case .connecting: return "Verifying · Square POS"
        case .connected: return "Connected · Square POS"
        case .failed: return "Couldn't connect · Square POS"
        }
    }
}
