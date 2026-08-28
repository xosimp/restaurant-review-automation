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

    private var canSubmit: Bool {
        !viewModel.isConnectingToast && !clientId.isEmpty && !clientSecret.isEmpty && !restaurantGuid.isEmpty
    }

    var body: some View {
        NavigationStack {
            ScrollView {
                VStack(alignment: .leading, spacing: 26) {
                    Text("Find these in Toast's admin under Toast Web → API Access. Ask your Toast rep if you don't see that option.")
                        .font(.cavnarBody(12))
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

                    if let error = viewModel.connectToastError {
                        Text(error).font(.cavnarBody(12)).foregroundStyle(Color.cavnarRed)
                    }

                    CavnarFormButtonPair { matchedWidth in
                        Button {
                            Task {
                                await viewModel.connectToast(
                                    clientId: clientId, clientSecret: clientSecret, restaurantGuid: restaurantGuid
                                )
                                if viewModel.connectToastSucceeded { dismiss() }
                            }
                        } label: {
                            if viewModel.isConnectingToast {
                                ProgressView().tint(Color.cavnarInk)
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
            .keyboardNavToolbar($focusedField)
        }
    }
}
