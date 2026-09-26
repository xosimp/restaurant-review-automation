import SwiftUI

struct TwoFactorView: View {
    @State private var viewModel: TwoFactorViewModel
    @FocusState private var isCodeFocused: Bool
    @FocusState private var isBackupFocused: Bool

    init(viewModel: TwoFactorViewModel) {
        _viewModel = State(initialValue: viewModel)
    }

    var body: some View {
        ZStack {
            Color.cavnarPaper.ignoresSafeArea()
            VStack(spacing: 24) {
                Spacer()

                VStack(spacing: 8) {
                    Text(viewModel.heading)
                        .font(.cavnarHeadline(26))
                        .foregroundStyle(Color.cavnarInk)
                    Text(viewModel.subheading)
                        .font(.cavnarBody(14))
                        .foregroundStyle(Color.cavnarInk3)
                        .multilineTextAlignment(.center)
                }

                if viewModel.useBackupCode {
                    // One field for a saved backup code ("7F3A-92C1"). Any
                    // case, dash or not — the server normalises it.
                    TextField("XXXX-XXXX", text: $viewModel.backupCode)
                        .font(.cavnarNumber(20, weight: 600))
                        .multilineTextAlignment(.center)
                        .textInputAutocapitalization(.characters)
                        .autocorrectionDisabled()
                        .keyboardType(.asciiCapable)
                        .submitLabel(.go)
                        .focused($isBackupFocused)
                        .cavnarTextFieldStyle()
                        .accessibilityLabel("Backup code")
                        .onSubmit { Task { await viewModel.submit() } }
                        .onChange(of: viewModel.backupCode) { _, _ in viewModel.errorMessage = nil }
                } else {
                    // Six cells, not a bare field — each digit pops into place,
                    // the active cell carries an ember caret, the row warms
                    // while verifying, and a wrong code shakes it. Submits on
                    // its own the moment the sixth digit lands.
                    CavnarCodeEntry(
                        code: $viewModel.code,
                        isVerifying: viewModel.isLoading,
                        isError: viewModel.errorMessage != nil,
                        focus: $isCodeFocused
                    )
                    .onChange(of: viewModel.code) { _, code in
                        if code.count < 6 { viewModel.errorMessage = nil }
                        if code.count == 6, viewModel.canSubmit {
                            Task { await viewModel.submit() }
                        }
                    }
                }

                Toggle("Remember this device for 30 days", isOn: $viewModel.rememberDevice)
                    .font(.cavnarBody(14.5))
                    .tint(Color.cavnarEmber)

                if let error = viewModel.errorMessage {
                    Text(error)
                        .font(.cavnarBody(14.5))
                        .foregroundStyle(Color.cavnarRed)
                        .multilineTextAlignment(.center)
                } else if let notice = viewModel.resendNotice {
                    Text(notice)
                        .font(.cavnarBody(14.5))
                        .foregroundStyle(Color.cavnarGreen)
                        .multilineTextAlignment(.center)
                }

                VStack(spacing: 12) {
                    Button {
                        Task { await viewModel.submit() }
                    } label: {
                        Group {
                            if viewModel.isLoading {
                                CavnarShimmerText(text: "Verifying…")
                            } else {
                                Text("Verify")
                            }
                        }
                        .frame(maxWidth: .infinity)
                    }
                    .buttonStyle(CavnarPrimaryButtonStyle(isDisabled: !viewModel.canSubmit))
                    .disabled(!viewModel.canSubmit)

                    if !viewModel.useBackupCode {
                        Button {
                            Task { await viewModel.resend() }
                        } label: {
                            Group {
                                if viewModel.isResending {
                                    CavnarShimmerText(text: "Sending…")
                                } else if viewModel.resendCooldown > 0 {
                                    Text("Resend code in \(viewModel.resendCooldown)s")
                                        .monospacedDigit()
                                } else {
                                    Text("Resend code")
                                }
                            }
                            .frame(maxWidth: .infinity)
                        }
                        .buttonStyle(CavnarSecondaryButtonStyle(isDisabled: !viewModel.canResend))
                        .disabled(!viewModel.canResend)
                    }

                    Button(viewModel.useBackupCode ? "Use the 6-digit code instead" : "Use a backup code instead") {
                        viewModel.toggleBackupCode()
                        if viewModel.useBackupCode { isBackupFocused = true } else { isCodeFocused = true }
                    }
                    .font(.cavnarBody(14.5, weight: 600))
                    .foregroundStyle(Color.cavnarEmber)
                    .padding(.top, 4)
                }

                Spacer()
                Spacer()
            }
            .padding(28)
        }
        .navigationBarTitleDisplayMode(.inline)
        .keyboardDoneToolbar { isCodeFocused = false; isBackupFocused = false }
        .onAppear { isCodeFocused = true }
    }
}
