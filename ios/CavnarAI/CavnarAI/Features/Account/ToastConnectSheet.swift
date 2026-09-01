import SwiftUI

private enum ToastConnectField: Hashable, CaseIterable {
    case clientId, clientSecret, restaurantGuid
}

/// Self-service version of the 3-field credential form Will otherwise
/// enters by hand in the admin panel (toast_client_id/toast_client_secret/
/// toast_restaurant_guid) — Toast has no OAuth, so a real API key pair is
/// the actual "connect" action here, not a redirect.
struct ToastConnectSheet: View {
    let viewModel: AccountViewModel
    @Environment(\.dismiss) private var dismiss
    @State private var clientId = ""
    @State private var clientSecret = ""
    @State private var restaurantGuid = ""
    @FocusState private var focusedField: ToastConnectField?
    // "Handshake" (see CavnarMotion): nil until the first attempt, then
    // marching dashes while the credentials are verified, solid ember on
    // success (held for a beat before the sheet dismisses itself), or a
    // stopped red line on failure until the next attempt.
    @State private var handshake: CavnarHandshakeState?

    private var canSubmit: Bool {
        !viewModel.isConnectingToast && !clientId.isEmpty && !clientSecret.isEmpty && !restaurantGuid.isEmpty
    }

    var body: some View {
        NavigationStack {
            ScrollView {
                VStack(alignment: .leading, spacing: 26) {
                    Text("Find these in Toast's admin under Toast Web → API Access. Ask your Toast rep if you don't see that option.")
                        .font(.cavnarBody(14))
                        .foregroundStyle(Color.cavnarInk3)

                    CavnarFloatingField(
                        icon: "key", placeholder: "Client ID", text: $clientId,
                        autocapitalization: .never, focus: $focusedField, field: .clientId
                    )
                    CavnarFloatingField(
                        icon: "lock", placeholder: "Client secret", text: $clientSecret,
                        isSecure: true, autocapitalization: .never, focus: $focusedField, field: .clientSecret
                    )
                    CavnarFloatingField(
                        icon: "number", placeholder: "Restaurant GUID", text: $restaurantGuid,
                        autocapitalization: .never, focus: $focusedField, field: .restaurantGuid
                    )

                    if let handshake {
                        CavnarHandshake(
                            providerSymbol: "fork.knife", providerTint: Color(red: 0.98, green: 0.35, blue: 0.15),
                            state: handshake, caption: handshakeCaption(handshake)
                        )
                        .transition(.opacity)
                    }

                    if let error = viewModel.connectToastError {
                        Text(error).font(.cavnarBody(14)).foregroundStyle(Color.cavnarRed)
                    }

                    CavnarFormButtonPair { matchedWidth in
                        Button {
                            Task {
                                withAnimation(.easeOut(duration: 0.25)) { handshake = .connecting }
                                await viewModel.connectToast(
                                    clientId: clientId, clientSecret: clientSecret, restaurantGuid: restaurantGuid
                                )
                                if viewModel.connectToastSucceeded {
                                    handshake = .connected
                                    try? await Task.sleep(for: .seconds(1.2))
                                    dismiss()
                                } else {
                                    handshake = .failed
                                }
                            }
                        } label: {
                            if viewModel.isConnectingToast {
                                CavnarShimmerText(text: "Connecting…")
                            } else {
                                Text("Connect Toast")
                            }
                        }
                        .buttonStyle(CavnarPrimaryButtonStyle(isDisabled: !canSubmit, matchedWidth: matchedWidth))
                        .disabled(!canSubmit)
                    } cancelAction: {
                        dismiss()
                    }
                    .padding(.top, 6)
                }
                .padding(20)
            }
            .cavnarModuleBackground()
            .navigationTitle("Connect Toast")
            .navigationBarTitleDisplayMode(.inline)
            .toolbar { cavnarTitleToolbar("Connect Toast") }
            .keyboardNavToolbar($focusedField)
        }
    }

    private func handshakeCaption(_ state: CavnarHandshakeState) -> String {
        switch state {
        case .connecting: return "Verifying · Toast POS"
        case .connected: return "Connected · Toast POS"
        case .failed: return "Couldn't connect · Toast POS"
        }
    }
}
