import SwiftUI

private enum UpdateEmailField: Hashable, CaseIterable {
    case email, password, code
}

struct UpdateEmailSheet: View {
    let viewModel: AccountViewModel
    @Environment(\.dismiss) private var dismiss
    @State private var newEmail = ""
    @State private var currentPassword = ""
    @State private var code = ""
    @State private var postedLabel: String?
    @FocusState private var focusedField: UpdateEmailField?

    private var canSubmit: Bool {
        !viewModel.isUpdatingEmail && newEmail.contains("@") && !currentPassword.isEmpty
            && (!viewModel.updateEmailNeedsCode || code.count >= 6)
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

                    // A code sent to the NEW address confirms the change
                    // (re-audit 10/20/26 R7-17).
                    if viewModel.updateEmailNeedsCode {
                        CavnarFloatingField(
                            icon: "number", placeholder: "6-digit code", text: $code,
                            keyboardType: .numberPad, textContentType: .oneTimeCode, autocapitalization: .never,
                            focus: $focusedField, field: .code
                        )
                    }
                    if let notice = viewModel.updateEmailNotice {
                        Text(notice).font(.cavnar(.body)).foregroundStyle(Color.cavnarInk)
                    }
                    if let error = viewModel.updateEmailError {
                        Text(error).font(.cavnar(.body)).foregroundStyle(Color.cavnarRedText)
                    }

                    // Plain full-width buttons, not a width-matched pair —
                    // this sheet is reached through the same two-level
                    // sheet chain (AccountView -> AccountProfileDetailView
                    // -> here) that first exposed the PreferenceKey width-
                    // matching bug on TwoFactorSetupSheet; device feedback
                    // confirmed the identical narrow-button symptom here.
                    VStack(spacing: 10) {
                        Button {
                            Task {
                                await viewModel.updateEmail(newEmail: newEmail, currentPassword: currentPassword,
                                                            code: viewModel.updateEmailNeedsCode ? code : nil)
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
                                    Text(viewModel.updateEmailNeedsCode ? "Confirm new email" : "Update email")
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
