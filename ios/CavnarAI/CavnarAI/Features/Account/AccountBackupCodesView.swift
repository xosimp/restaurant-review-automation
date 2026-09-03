import SwiftUI

/// Opened from Security's "Backup codes" row (only visible once 2FA is
/// on). Codes are shown in plaintext exactly once — either right after
/// 2FA is first enabled (TwoFactorSetupSheet's own step) or here, right
/// after a regenerate — since only the hash is ever persisted server-side.
/// Between those moments this sheet only ever shows a remaining count.
struct AccountBackupCodesView: View {
    let viewModel: AccountViewModel
    @State private var justGenerated: [String]?
    @State private var showingRegenerateConfirm = false

    var body: some View {
        NavigationStack {
            ScrollView {
                VStack(alignment: .leading, spacing: 22) {
                    if let codes = justGenerated {
                        Text("Save these codes somewhere safe — each works once to sign in if you lose access to email or text. Your old codes no longer work.")
                            .font(.cavnarBody(16))
                            .foregroundStyle(Color.cavnarInk3)
                        codesCard(codes)
                    } else {
                        Text("Backup codes let you sign in if you ever lose access to your email or phone during two-factor sign-in. Each code works once.")
                            .font(.cavnarBody(16))
                            .foregroundStyle(Color.cavnarInk3)
                        AccountSection(kicker: "Status") {
                            AccountKVRow(label: "Unused codes", showsDivider: false) {
                                AccountValue(text: viewModel.backupCodesRemaining.map(String.init) ?? "—", isNumber: true)
                            }
                        }
                    }

                    if let error = viewModel.backupCodesError {
                        Text(error).font(.cavnarBody(15)).foregroundStyle(Color.cavnarRed)
                    }

                    Button {
                        showingRegenerateConfirm = true
                    } label: {
                        Group {
                            if viewModel.isBackupCodesBusy {
                                CavnarShimmerText(text: "Generating…", color: Color.cavnarInk)
                            } else {
                                Text("Generate new codes")
                            }
                        }
                        .frame(maxWidth: .infinity)
                    }
                    .buttonStyle(CavnarSecondaryButtonStyle(isDisabled: viewModel.isBackupCodesBusy))
                    .disabled(viewModel.isBackupCodesBusy)
                }
                .frame(maxWidth: .infinity, alignment: .leading)
                .padding(20)
            }
            .accountSheetChrome("Backup Codes")
            .task { await viewModel.loadBackupCodesStatus() }
            .confirmationDialog(
                "Generate new backup codes?",
                isPresented: $showingRegenerateConfirm,
                titleVisibility: .visible
            ) {
                Button("Generate new codes", role: .destructive) {
                    Task {
                        if let codes = await viewModel.regenerateBackupCodes() {
                            Haptic.success()
                            justGenerated = codes
                        }
                    }
                }
                Button("Cancel", role: .cancel) {}
            } message: {
                Text("Any codes you saved before this stop working.")
            }
        }
    }

    private func codesCard(_ codes: [String]) -> some View {
        VStack(spacing: 0) {
            ForEach(Array(codes.enumerated()), id: \.offset) { index, code in
                HStack {
                    Text(code).font(.cavnarNumber(16, weight: 600)).foregroundStyle(Color.cavnarInk)
                    Spacer()
                }
                .padding(.vertical, 10)
                if index < codes.count - 1 { AccountRowDivider() }
            }
        }
        .cavnarCard()
    }
}
