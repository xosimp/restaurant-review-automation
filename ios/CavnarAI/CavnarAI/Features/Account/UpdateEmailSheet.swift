import SwiftUI

private enum UpdateEmailField: Hashable, CaseIterable {
    case email, password
}

struct UpdateEmailSheet: View {
    let viewModel: AccountViewModel
    @Environment(\.dismiss) private var dismiss
    @State private var newEmail = ""
    @State private var currentPassword = ""
    @State private var postedLabel: String?
    @FocusState private var focusedField: UpdateEmailField?

    private var canSubmit: Bool {
        !viewModel.isUpdatingEmail && newEmail.contains("@") && !currentPassword.isEmpty
    }

    var body: some View {
        NavigationStack {
            ScrollView {
                VStack(alignment: .leading, spacing: 26) {
                    CavnarFloatingField(
                        icon: "envelope", placeholder: "New email", text: $newEmail,
                        keyboardType: .emailAddress, textContentType: .emailAddress, autocapitalization: .never,
                        focus: $focusedField, field: .email
                    )
                    CavnarFloatingField(
                        icon: "lock", placeholder: "Current password", text: $currentPassword,
                        isSecure: true, textContentType: .password,
                        focus: $focusedField, field: .password
                    )

                    if let error = viewModel.updateEmailError {
                        Text(error).font(.cavnarBody(14)).foregroundStyle(Color.cavnarRed)
                    }

                    CavnarFormButtonPair { matchedWidth in
                        Button {
                            Task {
                                await viewModel.updateEmail(newEmail: newEmail, currentPassword: currentPassword)
                                if viewModel.updateEmailSucceeded {
                                    Haptic.success()
                                    postedLabel = "Email updated"
                                }
                            }
                        } label: {
                            if viewModel.isUpdatingEmail {
                                CavnarShimmerText(text: "Updating…", color: Color.cavnarInk)
                            } else {
                                Text("Update email")
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
            .navigationTitle("Update Email")
            .navigationBarTitleDisplayMode(.inline)
            .toolbar { cavnarTitleToolbar("Update Email") }
            .keyboardNavToolbar($focusedField)
            .cavnarPostedOverlay(postedLabel) { dismiss() }
        }
    }
}
