import SwiftUI

struct TwoFactorView: View {
    @State private var viewModel: TwoFactorViewModel
    @FocusState private var isCodeFocused: Bool

    init(viewModel: TwoFactorViewModel) {
        _viewModel = State(initialValue: viewModel)
    }

    var body: some View {
        ZStack {
            Color.cavnarPaper.ignoresSafeArea()
            VStack(spacing: 24) {
                Spacer()

                VStack(spacing: 8) {
                    Text("Check your email")
                        .font(.cavnarHeadline(26))
                        .foregroundStyle(Color.cavnarInk)
                    Text("We sent a 6-digit code to \(viewModel.maskedEmail)")
                        .font(.cavnarBody(14))
                        .foregroundStyle(Color.cavnarInk3)
                        .multilineTextAlignment(.center)
                }

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

                Toggle("Remember this device for 30 days", isOn: $viewModel.rememberDevice)
                    .font(.cavnarBody(14.5))
                    .tint(Color.cavnarEmber)

                if let error = viewModel.errorMessage {
                    Text(error)
                        .font(.cavnarBody(14.5))
                        .foregroundStyle(Color.cavnarRed)
                        .multilineTextAlignment(.center)
                }

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

                Spacer()
                Spacer()
            }
            .padding(28)
        }
        .navigationBarTitleDisplayMode(.inline)
        .keyboardDoneToolbar { isCodeFocused = false }
        .onAppear { isCodeFocused = true }
    }
}
