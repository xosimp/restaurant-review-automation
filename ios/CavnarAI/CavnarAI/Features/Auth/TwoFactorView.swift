import SwiftUI

struct TwoFactorView: View {
    @State private var viewModel: TwoFactorViewModel

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

                TextField("6-digit code", text: $viewModel.code)
                    .keyboardType(.numberPad)
                    .multilineTextAlignment(.center)
                    .font(.cavnarNumber(24, weight: 600))
                    .cavnarTextFieldStyle()

                Toggle("Remember this device for 30 days", isOn: $viewModel.rememberDevice)
                    .font(.cavnarBody(13))
                    .tint(Color.cavnarEmber)

                if let error = viewModel.errorMessage {
                    Text(error)
                        .font(.cavnarBody(13))
                        .foregroundStyle(Color.cavnarRed)
                        .multilineTextAlignment(.center)
                }

                Button {
                    Task { await viewModel.submit() }
                } label: {
                    if viewModel.isLoading {
                        ProgressView().tint(.white)
                    } else {
                        Text("Verify")
                    }
                }
                .buttonStyle(CavnarPrimaryButtonStyle(isDisabled: !viewModel.canSubmit))
                .disabled(!viewModel.canSubmit)

                Spacer()
                Spacer()
            }
            .padding(28)
        }
        .navigationBarTitleDisplayMode(.inline)
    }
}
