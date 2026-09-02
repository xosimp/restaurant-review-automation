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
                        Text(error).font(.cavnarBody(15)).foregroundStyle(Color.cavnarRed)
                    }

                    // Plain full-width buttons, not CavnarFormButtonPair —
                    // this sheet is reached through the same two-level
                    // sheet chain (AccountView -> AccountProfileDetailView
                    // -> here) that first exposed the PreferenceKey width-
                    // matching bug on TwoFactorSetupSheet; device feedback
                    // confirmed the identical narrow-button symptom here.
                    VStack(spacing: 10) {
                        Button {
                            Task {
                                await viewModel.updateEmail(newEmail: newEmail, currentPassword: currentPassword)
                                if viewModel.updateEmailSucceeded {
                                    Haptic.success()
                                    postedLabel = "Email updated"
                                }
                            }
                        } label: {
                            Group {
                                if viewModel.isUpdatingEmail {
                                    CavnarShimmerText(text: "Updating…", color: Color.cavnarInk)
                                } else {
                                    Text("Update email")
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
            .navigationTitle("Update Email")
            .navigationBarTitleDisplayMode(.inline)
            .toolbar { cavnarTitleToolbar("Update Email") }
            .keyboardNavToolbar($focusedField)
            .cavnarPostedOverlay(postedLabel) { dismiss() }
        }
    }
}
