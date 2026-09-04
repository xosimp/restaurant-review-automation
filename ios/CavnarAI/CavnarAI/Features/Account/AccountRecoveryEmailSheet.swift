import SwiftUI

/// Opened from Security's "Recovery email" row. A second address that can
/// receive a password-reset link if the sign-in email is ever lost —
/// verified with a code before it counts, so a typo can't become the
/// address that controls the account.
struct AccountRecoveryEmailSheet: View {
    let viewModel: AccountViewModel
    @Environment(\.dismiss) private var dismiss
    @State private var email = ""
    @State private var code = ""
    @State private var awaitingCode = false
    @State private var postedLabel: String?
    @State private var postedTone: CavnarPostedTone = .success
    @State private var confirmingRemove = false
    private enum Field: Hashable { case email, code }
    @FocusState private var focused: Field?

    private var current: String? { viewModel.summary?.account.recoveryEmail }
    private var pending: String? { viewModel.summary?.account.recoveryEmailPending }

    var body: some View {
        NavigationStack {
            ScrollView {
                VStack(alignment: .leading, spacing: 22) {
                    AccountHero(title: current == nil ? "Add a recovery email" : "Recovery email") {
                        GlowBadge(systemImage: "envelope.badge.shield.half.filled", size: 64)
                    } subtitle: {
                        Text(current ?? "A second way back into your account")
                    }

                    Text("If you ever lose access to your sign-in email, a password-reset link can go here instead. We'll send a code to confirm it's really yours.")
                        .font(.cavnarBody(15))
                        .foregroundStyle(Color.cavnarInk3)

                    if let error = viewModel.recoveryEmailError {
                        Text(error).font(.cavnarBody(15)).foregroundStyle(Color.cavnarRed)
                    }

                    if awaitingCode {
                        AccountSection(kicker: "Code sent to \(pending ?? email)") {
                            AccountField(label: "6-digit code", text: $code, focus: $focused, field: .code, keyboardType: .numberPad, isNumber: true, showsDivider: false)
                        }
                        VStack(spacing: 10) {
                            Button {
                                Task {
                                    if await viewModel.verifyRecoveryEmail(code: code) {
                                        Haptic.success()
                                        postedTone = .success
                                        postedLabel = "Recovery email set"
                                    }
                                }
                            } label: {
                                Group {
                                    if viewModel.isRecoveryEmailBusy { CavnarShimmerText(text: "Checking…") } else { Text("Confirm") }
                                }
                                .frame(maxWidth: .infinity)
                            }
                            .buttonStyle(CavnarPrimaryButtonStyle(isDisabled: code.count != 6 || viewModel.isRecoveryEmailBusy))
                            .disabled(code.count != 6 || viewModel.isRecoveryEmailBusy)
                            Button {
                                Haptic.light()
                                awaitingCode = false
                                code = ""
                            } label: {
                                Text("Use a different address").frame(maxWidth: .infinity)
                            }
                            .buttonStyle(CavnarSecondaryButtonStyle())
                        }
                    } else {
                        AccountSection(kicker: current == nil ? "Address" : "Change address") {
                            AccountField(label: "Recovery email", text: $email, focus: $focused, field: .email, keyboardType: .emailAddress, showsDivider: false)
                        }
                        VStack(spacing: 10) {
                            Button {
                                Task {
                                    if await viewModel.startRecoveryEmail(email) {
                                        Haptic.success()
                                        awaitingCode = true
                                        focused = .code
                                    }
                                }
                            } label: {
                                Group {
                                    if viewModel.isRecoveryEmailBusy { CavnarShimmerText(text: "Sending…") } else { Text("Send code") }
                                }
                                .frame(maxWidth: .infinity)
                            }
                            .buttonStyle(CavnarPrimaryButtonStyle(isDisabled: !email.contains("@") || viewModel.isRecoveryEmailBusy))
                            .disabled(!email.contains("@") || viewModel.isRecoveryEmailBusy)
                            if current != nil {
                                Button {
                                    Haptic.light()
                                    confirmingRemove = true
                                } label: {
                                    Text("Remove recovery email").frame(maxWidth: .infinity)
                                }
                                .buttonStyle(CavnarSecondaryButtonStyle())
                            }
                        }
                    }
                }
                .frame(maxWidth: .infinity, alignment: .leading)
                .padding(20)
            }
            .accountSheetChrome("Recovery Email")
            .keyboardDoneToolbar { focused = nil }
            .cavnarPostedOverlay(postedLabel, tone: postedTone) { dismiss() }
            .onAppear {
                if let pending, !pending.isEmpty { email = pending; awaitingCode = true }
            }
            .confirmationDialog("Remove the recovery email?", isPresented: $confirmingRemove, titleVisibility: .visible) {
                Button("Remove", role: .destructive) {
                    Task {
                        if await viewModel.removeRecoveryEmail() {
                            Haptic.success()
                            postedTone = .removed
                            postedLabel = "Recovery email removed"
                        }
                    }
                }
                Button("Cancel", role: .cancel) {}
            }
        }
    }
}
