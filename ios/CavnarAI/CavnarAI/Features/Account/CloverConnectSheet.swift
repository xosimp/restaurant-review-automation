import SwiftUI

private enum CloverConnectField: Hashable, CaseIterable {
    case merchantId, apiToken
}

/// Self-service Clover connect — merchant ID + API token, the same pair
/// the admin panel and the web dashboard already set. See
/// SquareConnectSheet for why a credential pair (not OAuth) is the connect
/// action here, and why the credentials are verified before being stored.
struct CloverConnectSheet: View {
    let viewModel: AccountViewModel
    @Environment(\.dismiss) private var dismiss
    @State private var merchantId = ""
    @State private var apiToken = ""
    @FocusState private var focusedField: CloverConnectField?
    @State private var handshake: CavnarHandshakeState?

    private var canSubmit: Bool {
        !viewModel.isConnectingClover && !merchantId.isEmpty && !apiToken.isEmpty
    }

    var body: some View {
        NavigationStack {
            ScrollView {
                VStack(alignment: .leading, spacing: 26) {
                    Text("Find these in Clover's Developer Dashboard — your merchant ID sits in the URL of your Clover dashboard, and API tokens are under Setup → API Tokens.")
                        .font(.cavnarBody(15))
                        .foregroundStyle(Color.cavnarInk3)

                    CavnarFloatingField(
                        icon: "number", placeholder: "Merchant ID", text: $merchantId,
                        autocapitalization: .never, focus: $focusedField, field: .merchantId
                    )
                    CavnarFloatingField(
                        icon: "key", placeholder: "API token", text: $apiToken,
                        isSecure: true, autocapitalization: .never, focus: $focusedField, field: .apiToken
                    )

                    if let handshake {
                        CavnarHandshake(
                            providerSymbol: "leaf.fill",
                            providerTint: Color(red: 0.0, green: 0.58, blue: 0.32),
                            state: handshake, caption: handshakeCaption(handshake)
                        )
                        .transition(.opacity)
                    }

                    if let error = viewModel.connectCloverError {
                        Text(error).font(.cavnarBody(15)).foregroundStyle(Color.cavnarRed)
                    }

                    VStack(spacing: 10) {
                        Button {
                            // Resign focus before anything can tear this sheet
                            // down (see closeAfterKeyboard below), and so the
                            // handshake animation isn't behind the keyboard.
                            focusedField = nil
                            Task {
                                withAnimation(.easeOut(duration: 0.25)) { handshake = .connecting }
                                await viewModel.connectClover(merchantId: merchantId, apiToken: apiToken)
                                if viewModel.connectCloverSucceeded {
                                    handshake = .connected
                                    try? await Task.sleep(for: .seconds(1.2))
                                    closeAfterKeyboard()
                                } else {
                                    handshake = .failed
                                }
                            }
                        } label: {
                            Group {
                                if viewModel.isConnectingClover {
                                    CavnarShimmerText(text: "Connecting…")
                                } else {
                                    Text("Connect Clover")
                                }
                            }
                            .frame(maxWidth: .infinity)
                        }
                        .buttonStyle(CavnarPrimaryButtonStyle(isDisabled: !canSubmit))
                        .disabled(!canSubmit)

                        Button {
                            closeAfterKeyboard()
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
            .navigationTitle("Connect Clover")
            .navigationBarTitleDisplayMode(.inline)
            .toolbar { cavnarTitleToolbar("Connect Clover") }
            .keyboardNavToolbar($focusedField)
        }
    }

    /// Drop the keyboard, let it actually go, THEN dismiss. See
    /// SquareConnectSheet.closeAfterKeyboard() for the full reasoning:
    /// dismissing while a field is still first responder can leave the
    /// `.keyboard`-placement toolbar (hosted by UIKit's input-accessory
    /// system, outside this sheet's hierarchy) reading a FocusState whose
    /// storage is already gone.
    private func closeAfterKeyboard() {
        focusedField = nil
        Task { @MainActor in
            try? await Task.sleep(for: .milliseconds(60))
            dismiss()
        }
    }

    private func handshakeCaption(_ state: CavnarHandshakeState) -> String {
        switch state {
        case .connecting: return "Verifying · Clover POS"
        case .connected: return "Connected · Clover POS"
        case .failed: return "Couldn't connect · Clover POS"
        }
    }
}
