import SwiftUI

struct LoginView: View {
    @Environment(SessionStore.self) private var sessionStore
    @State private var viewModel: LoginViewModel

    init(sessionStore: SessionStore) {
        _viewModel = State(initialValue: LoginViewModel(sessionStore: sessionStore))
    }

    var body: some View {
        NavigationStack {
            ZStack {
                Color.cavnarPaper.ignoresSafeArea()
                VStack(spacing: 28) {
                    Spacer()

                    VStack(spacing: 6) {
                        Text("Cavnar AI")
                            .font(.cavnarHeadline(36))
                            .foregroundStyle(Color.cavnarInk)
                        Text("Sign in to your restaurant")
                            .font(.cavnarBody(14))
                            .foregroundStyle(Color.cavnarInk3)
                    }

                    VStack(spacing: 12) {
                        TextField("Username", text: $viewModel.username)
                            .textInputAutocapitalization(.never)
                            .autocorrectionDisabled()
                            .cavnarTextFieldStyle()
                        SecureField("Password", text: $viewModel.password)
                            .cavnarTextFieldStyle()
                    }

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
                            Text("Sign In")
                        }
                    }
                    .buttonStyle(CavnarPrimaryButtonStyle(isDisabled: !viewModel.canSubmit))
                    .disabled(!viewModel.canSubmit)

                    Spacer()
                    Spacer()
                }
                .padding(28)
            }
            .navigationDestination(
                isPresented: Binding(
                    get: { viewModel.twoFactorPendingToken != nil },
                    set: { isPresented in
                        if !isPresented { viewModel.twoFactorPendingToken = nil }
                    }
                )
            ) {
                if let pendingToken = viewModel.twoFactorPendingToken {
                    TwoFactorView(
                        viewModel: TwoFactorViewModel(
                            sessionStore: sessionStore,
                            pendingToken: pendingToken,
                            maskedEmail: viewModel.twoFactorMaskedEmail ?? ""
                        )
                    )
                }
            }
        }
    }
}
